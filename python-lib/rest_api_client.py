import requests
import time
import re
from pagination import Pagination
from safe_logger import SafeLogger
from loop_detector import LoopDetector
from dku_utils import get_dku_key_values
from dku_constants import DKUConstants


logger = SafeLogger("rest-api plugin", forbiden_keys=["token", "password"])


def template_dict(dictionnary, **kwargs):
    """ Recurses into dictionnary and replace template {{keys}} with the matching values present in the kwargs dictionnary"""
    ret = dict.copy(dictionnary)
    for key in ret:
        if isinstance(ret[key], dict):
            ret[key] = template_dict(ret[key], **kwargs)
        if isinstance(ret[key], str):
            ret[key] = format_template(ret[key], **kwargs)
            return ret
    return ret


def format_template(template, **kwargs):
    """ Replace {{keys}} elements in template with the matching value in the kwargs dictionnary"""
    placeholders = re.findall(r'{{([a-zA-Z\-\_]*)}}', template)
    formated = template
    for placeholder in placeholders:
        replacement = kwargs.get(placeholder, "")
        formated = formated.replace("{{{{{}}}}}".format(placeholder), str(replacement))
    return formated


class RestAPIClientError(ValueError):
    pass


class RestAPIClient(object):

    def __init__(self, credential, endpoint, custom_key_values={}):
        logger.info("Initialising RestAPIClient, credential={}, endpoint={}".format(logger.filter_secrets(credential), endpoint))

        #  presets_variables contains all variables available in templates using the {{variable_name}} notation
        self.presets_variables = {}
        self.presets_variables.update(endpoint)
        self.presets_variables.update(credential)
        self.presets_variables.update(custom_key_values)

        #  requests_kwargs contains **kwargs used for requests
        self.requests_kwargs = {}

        self.endpoint_query_string = endpoint.get("endpoint_query_string", [])
        user_defined_keys = credential.get("user_defined_keys", [])
        self.user_defined_keys = self.get_params(user_defined_keys, self.presets_variables)
        self.presets_variables.update(self.user_defined_keys)

        endpoint_url = endpoint.get("endpoint_url", "")
        self.endpoint_url = format_template(endpoint_url, **self.presets_variables)
        self.http_method = endpoint.get("http_method", "GET")

        endpoint_headers = endpoint.get("endpoint_headers", "")
        self.endpoint_headers = self.get_params(endpoint_headers, self.presets_variables)

        self.params = self.get_params(self.endpoint_query_string, self.presets_variables)

        self.extraction_key = endpoint.get("extraction_key", None)

        self.set_login(credential)

        self.requests_kwargs.update({"headers": self.endpoint_headers})
        self.ignore_ssl_check = endpoint.get("ignore_ssl_check", False)
        if self.ignore_ssl_check:
            self.requests_kwargs.update({"verify": False})
        else:
            self.requests_kwargs.update({"verify": True})
        self.timeout = endpoint.get("timeout", -1)
        if self.timeout > 0:
            self.requests_kwargs.update({"timeout": self.timeout})

        self.requests_kwargs.update({"params": self.params})
        self.pagination = Pagination()
        next_page_url_key = endpoint.get("next_page_url_key", "").split('.')
        top_key = endpoint.get("top_key")
        skip_key = endpoint.get("skip_key")
        pagination_type = endpoint.get("pagination_type", "na")
        self.pagination.configure_paging(
            skip_key=skip_key,
            limit_key=top_key,
            next_page_key=next_page_url_key,
            url=self.endpoint_url,
            pagination_type=pagination_type
        )
        self.last_interaction = None
        self.requests_per_minute = endpoint.get("requests_per_minute", -1)
        if self.requests_per_minute > 0:
            self.time_between_requests = 60 / self.requests_per_minute
        else:
            self.time_between_requests = None
        self.time_last_request = None
        self.loop_detector = LoopDetector()
        body_format = endpoint.get("body_format", None)
        if body_format == DKUConstants.RAW_BODY_FORMAT:
            text_body = endpoint.get("text_body", "")
            self.requests_kwargs.update({"data": text_body})
        elif body_format in [DKUConstants.FORM_DATA_BODY_FORMAT]:
            key_value_body = endpoint.get("key_value_body", {})
            self.requests_kwargs.update({"json": get_dku_key_values(key_value_body)})

    def set_login(self, credential):
        login_type = credential.get("login_type", "no_auth")
        if login_type == "basic_login":
            self.username = credential.get("username", "")
            self.password = credential.get("password", "")
            self.auth = (self.username, self.password)
            self.requests_kwargs.update({"auth": self.auth})
        if login_type == "token":
            self.token = credential.get("token", "")
        if login_type == "oauth_2_token":
            self.token = credential.get("token", "")
            bearer_template = credential.get("bearer_template", "Bearer {{token}}")
            self.endpoint_headers.update({"Authentication": bearer_template})
        if login_type == "api_key":
            self.api_key_name = credential.get("api_key_name", "")
            self.api_key_value = credential.get("api_key_value", "")
            self.api_key_destination = credential.get("api_key_destination", "header")
            if self.api_key_destination == "header":
                self.endpoint_headers.update({self.api_key_name: self.api_key_value})
            else:
                self.params.update({self.api_key_name: self.api_key_value})

    def get(self, url, can_raise_exeption=True, **kwargs):
        json_response = self.request("GET", url, can_raise_exeption=can_raise_exeption, **kwargs)
        return json_response

    def request(self, method, url, can_raise_exeption=True, **kwargs):
        logger.info("Accessing endpoint {} with params={}".format(url, kwargs.get("params")))
        self.enforce_throttling()
        kwargs = template_dict(kwargs, **self.presets_variables)
        if self.loop_detector.is_stuck_in_loop(url, kwargs.get("params", {}), kwargs.get("headers", {})):
            raise RestAPIClientError("The rest-api plugin is stuck in a loop. Please check the pagination parameters.")
        try:
            response = requests.request(method, url, **kwargs)
        except Exception as err:
            self.pagination.is_last_batch_empty = True
            error_message = "Error: {}".format(err)
            if can_raise_exeption:
                raise RestAPIClientError(error_message)
            else:
                return {"error": error_message}
        self.time_last_request = time.time()
        if response.status_code >= 400:
            error_message = "Error {}: {}".format(response.status_code, response.content)
            self.pagination.is_last_batch_empty = True
            if can_raise_exeption:
                raise RestAPIClientError(error_message)
            else:
                return {"error": error_message}
        json_response = response.json()
        self.pagination.update_next_page(json_response)
        return json_response

    def paginated_get(self, can_raise_exeption=True):
        pagination_params = self.pagination.get_params()
        params = self.requests_kwargs.get("params")
        params.update(pagination_params)
        self.requests_kwargs.update({"params": params})
        return self.get(self.pagination.get_next_page_url(), can_raise_exeption, **self.requests_kwargs)

    def paginated_api_call(self, can_raise_exeption=True):
        if self.http_method == "GET":
            return self.paginated_get(can_raise_exeption=True)
        return self.request(self.http_method, self.pagination.get_next_page_url(), can_raise_exeption, **self.requests_kwargs)

    @staticmethod
    def get_params(endpoint_query_string, keywords):
        templated_query_string = get_dku_key_values(endpoint_query_string)
        ret = {}
        for key in templated_query_string:
            ret.update({key: format_template(templated_query_string.get(key, ""), **keywords)})
        return ret

    def has_more_data(self):
        if not self.pagination.is_paging_started:
            self.start_paging()
        return self.pagination.has_next_page()

    def start_paging(self):
        self.pagination.reset_paging(counting_key=self.extraction_key, url=self.endpoint_url)

    def enforce_throttling(self):
        if self.time_between_requests and self.time_last_request:
            current_time = time.time()
            time_since_last_resquests = current_time - self.time_last_request
            if time_since_last_resquests < self.time_between_requests:
                logger.info("Enforcing {}s throttling".format(self.time_between_requests - time_since_last_resquests))
                time.sleep(self.time_between_requests - time_since_last_resquests)
