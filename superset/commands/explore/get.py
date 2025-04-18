# Licensed to the Apache Software Foundation (ASF) under one
# or more contributor license agreements.  See the NOTICE file
# distributed with this work for additional information
# regarding copyright ownership.  The ASF licenses this file
# to you under the Apache License, Version 2.0 (the
# "License"); you may not use this file except in compliance
# with the License.  You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing,
# software distributed under the License is distributed on an
# "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
# KIND, either express or implied.  See the License for the
# specific language governing permissions and limitations
# under the License.
import contextlib
import logging
from abc import ABC
from typing import Any, cast, Optional

from flask import request
from flask_babel import lazy_gettext as _
from sqlalchemy.exc import SQLAlchemyError

from superset.commands.base import BaseCommand
from superset.commands.explore.form_data.get import GetFormDataCommand
from superset.commands.explore.form_data.parameters import (
    CommandParameters as FormDataCommandParameters,
)
from superset.commands.explore.parameters import CommandParameters
from superset.commands.explore.permalink.get import GetExplorePermalinkCommand
from superset.connectors.sqla.models import BaseDatasource, SqlaTable
from superset.daos.datasource import DatasourceDAO
from superset.daos.exceptions import DatasourceNotFound
from superset.exceptions import SupersetException
from superset.explore.exceptions import WrongEndpointError
from superset.explore.permalink.exceptions import ExplorePermalinkGetFailedError
from superset.extensions import security_manager
from superset.utils import core as utils, json
from superset.views.utils import (
    get_datasource_info,
    get_form_data,
    sanitize_datasource_data,
)

logger = logging.getLogger(__name__)


class GetExploreCommand(BaseCommand, ABC):
    def __init__(
        self,
        params: CommandParameters,
    ) -> None:
        self._permalink_key = params.permalink_key
        self._form_data_key = params.form_data_key
        self._datasource_id = params.datasource_id
        self._datasource_type = params.datasource_type
        self._slice_id = params.slice_id

    # pylint: disable=too-many-locals,too-many-branches,too-many-statements
    def run(self) -> Optional[dict[str, Any]]:  # noqa: C901
        initial_form_data = {}
        if self._permalink_key is not None:
            command = GetExplorePermalinkCommand(self._permalink_key)
            permalink_value = command.run()
            if not permalink_value:
                raise ExplorePermalinkGetFailedError()
            state = permalink_value["state"]
            initial_form_data = state["formData"]
            url_params = state.get("urlParams")
            if url_params:
                initial_form_data["url_params"] = dict(url_params)
        elif self._form_data_key:
            parameters = FormDataCommandParameters(key=self._form_data_key)
            value = GetFormDataCommand(parameters).run()
            initial_form_data = json.loads(value) if value else {}

        message = None

        if not initial_form_data:
            if self._slice_id:
                initial_form_data["slice_id"] = self._slice_id
                if self._form_data_key:
                    message = _(
                        "Form data not found in cache, reverting to chart metadata."
                    )
            elif self._datasource_id:
                initial_form_data["datasource"] = (
                    f"{self._datasource_id}__{self._datasource_type}"
                )
                if self._form_data_key:
                    message = _(
                        "Form data not found in cache, reverting to dataset metadata."
                    )

        form_data, slc = get_form_data(
            slice_id=self._slice_id,
            use_slice_data=True,
            initial_form_data=initial_form_data,
        )
        try:
            self._datasource_id, self._datasource_type = get_datasource_info(
                self._datasource_id, self._datasource_type, form_data
            )
        except SupersetException:
            self._datasource_id = None
            # fallback unknown datasource to table type
            self._datasource_type = SqlaTable.type

        datasource: Optional[BaseDatasource] = None

        if self._datasource_id is not None:
            with contextlib.suppress(DatasourceNotFound):
                datasource = DatasourceDAO.get_datasource(
                    cast(str, self._datasource_type), self._datasource_id
                )

        datasource_name = _("[Missing Dataset]")

        if datasource:
            datasource_name = datasource.name
            security_manager.raise_for_access(datasource=datasource)

        viz_type = form_data.get("viz_type")
        if not viz_type and datasource and datasource.default_endpoint:
            raise WrongEndpointError(redirect=datasource.default_endpoint)

        form_data["datasource"] = (
            str(self._datasource_id) + "__" + cast(str, self._datasource_type)
        )

        # On explore, merge legacy/extra filters and URL params into the form data
        utils.convert_legacy_filters_into_adhoc(form_data)
        utils.merge_extra_filters(form_data)
        utils.merge_request_params(form_data, request.args)

        # TODO: this is a dummy placeholder - should be refactored to being just `None`
        datasource_data: dict[str, Any] = {
            "type": self._datasource_type,
            "name": datasource_name,
            "columns": [],
            "metrics": [],
            "database": {"id": 0, "backend": ""},
        }
        try:
            if datasource:
                datasource_data = datasource.data
        except SupersetException as ex:
            message = ex.message
        except SQLAlchemyError:
            message = "SQLAlchemy error"

        metadata = None

        if slc:
            metadata = {
                "created_on_humanized": slc.created_on_humanized,
                "changed_on_humanized": slc.changed_on_humanized,
                "owners": [owner.get_full_name() for owner in slc.owners],
                "dashboards": [
                    {"id": dashboard.id, "dashboard_title": dashboard.dashboard_title}
                    for dashboard in slc.dashboards
                ],
            }
            if slc.created_by:
                metadata["created_by"] = slc.created_by.get_full_name()
            if slc.changed_by:
                metadata["changed_by"] = slc.changed_by.get_full_name()

        return {
            "dataset": sanitize_datasource_data(datasource_data),
            "form_data": form_data,
            "slice": slc.data if slc else None,
            "message": message,
            "metadata": metadata,
        }

    def validate(self) -> None:
        pass
