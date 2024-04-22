import functools
import json
import re
import sys
import time
from typing import List, Mapping
import uuid

from jinja2 import Template
from openai import APIConnectionError, APIStatusError, APITimeoutError, BadRequestError, OpenAIError, RateLimitError

from promptflow.connections import AzureOpenAIConnection, OpenAIConnection
from promptflow.contracts.types import PromptTemplate
from promptflow.exceptions import SystemErrorException, UserErrorException
from promptflow.tools.exception import (
    ToolValidationError,
    ChatAPIAssistantRoleInvalidFormat,
    ChatAPIFunctionRoleInvalidFormat,
    ChatAPIToolRoleInvalidFormat,
    ChatAPIInvalidFunctions,
    ChatAPIInvalidRole,
    ChatAPIInvalidTools,
    ExceedMaxRetryTimes,
    FunctionCallNotSupportedInStreamMode,
    InvalidConnectionType,
    JinjaTemplateError,
    ListDeploymentsError,
    LLMError,
    ParseConnectionError,
    WrappedOpenAIError,
)

GPT4V_VERSION = "vision-preview"
VALID_ROLES = ["system", "user", "assistant", "function", "tool"]


class Deployment:
    def __init__(self, name: str, model_name: str, version: str):
        self.name = name
        self.model_name = model_name
        self.version = version


class ChatInputList(list):
    """
    ChatInputList is a list of ChatInput objects. It is used to override the __str__ method of list to return a string
    that can be easily parsed as message list.
    """

    def __init__(self, iterable=None):
        super().__init__(iterable or [])

    def __str__(self):
        return "\n".join(map(str, self))


def validate_role(role: str, valid_roles: List[str] = None):
    if not valid_roles:
        valid_roles = VALID_ROLES

    if role not in valid_roles:
        valid_roles_str = ','.join([f'\'{role}:\\n\'' for role in valid_roles])
        error_message = (
            f"The Chat API requires a specific format for prompt definition, and the prompt should include separate "
            f"lines as role delimiters: {valid_roles_str}. Current parsed role '{role}'"
            f" does not meet the requirement. If you intend to use the Completion API, please select the appropriate"
            f" API type and deployment name. If you do intend to use the Chat API, please refer to the guideline at "
            f"https://aka.ms/pfdoc/chat-prompt or view the samples in our gallery that contain 'Chat' in the name."
        )
        raise ChatAPIInvalidRole(message=error_message)


def validate_function(common_tsg, i, function, expection: ToolValidationError):
    # validate if the function is a dict
    if not isinstance(function, dict):
        raise expection(message=f"function {i} '{function}' is not a dict. {common_tsg}")
    # validate if has required keys
    for key in ["name", "parameters"]:
        if key not in function.keys():
            raise expection(
                message=f"function {i} '{function}' does not have '{key}' property. {common_tsg}"
            )
    # validate if the parameters is a dict
    if not isinstance(function["parameters"], dict):
        raise expection(
            message=f"function {i} '{function['name']}' parameters '{function['parameters']}' "
            f"should be described as a JSON Schema object. {common_tsg}"
        )
    # validate if the parameters has required keys
    for key in ["type", "properties"]:
        if key not in function["parameters"].keys():
            raise expection(
                message=f"function {i} '{function['name']}' parameters '{function['parameters']}' "
                f"does not have '{key}' property. {common_tsg}"
            )
    # validate if the parameters type is object
    if function["parameters"]["type"] != "object":
        raise expection(
            message=f"function {i} '{function['name']}' parameters 'type' " f"should be 'object'. {common_tsg}"
        )
    # validate if the parameters properties is a dict
    if not isinstance(function["parameters"]["properties"], dict):
        raise expection(
            message=f"function {i} '{function['name']}' parameters 'properties' "
            f"should be described as a JSON Schema object. {common_tsg}"
        )


def validate_functions(functions):
    function_example = json.dumps({
        "name": "function_name",
        "parameters": {
            "type": "object",
            "properties": {
                "parameter_name": {
                    "type": "integer",
                    "description": "parameter_description"
                }
            }
        },
        "description": "function_description"
    })
    common_tsg = f"Here is a valid function example: {function_example}. See more details at " \
                 "https://platform.openai.com/docs/api-reference/chat/create#chat/create-functions " \
                 "or view sample 'How to use functions with chat models' in our gallery."
    if len(functions) == 0:
        raise ChatAPIInvalidFunctions(message=f"functions cannot be an empty list. {common_tsg}")
    else:
        for i, function in enumerate(functions):
            validate_function(common_tsg, i, function, ChatAPIInvalidFunctions)


def validate_tools(tools):
    tool_example = json.dumps(
        {
            "type": "function",
            "function": {
                "name": "get_current_weather",
                "description": "Get the current weather in a given location",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "location": {
                            "type": "string",
                            "description": "The city and state, e.g. San Francisco, CA",
                        },
                        "unit": {"type": "string", "enum": ["celsius", "fahrenheit"]},
                    },
                    "required": ["location"],
                },
            },
        }
    )
    common_tsg = (
        f"Here is a valid tool example: {tool_example}. See more details at "
        "https://platform.openai.com/docs/api-reference/chat/create"
    )

    if len(tools) == 0:
        raise ChatAPIInvalidTools(message=f"tools cannot be an empty list. {common_tsg}")
    for i, tool in enumerate(tools):
        # validate if the tool is a dict
        if not isinstance(tool, dict):
            raise ChatAPIInvalidTools(message=f"tool {i} '{tool}' is not a dict. {common_tsg}")
        # validate if has required keys
        for key in ["type", "function"]:
            if key not in tool.keys():
                raise ChatAPIInvalidTools(
                    message=f"tool {i} '{tool}' does not have '{key}' property. {common_tsg}")
        validate_function(common_tsg, i, tool["function"], ChatAPIInvalidTools)


def try_parse_name_and_content(role_prompt):
    # customer can add ## in front of name/content for markdown highlight.
    # and we still support name/content without ## prefix for backward compatibility.
    pattern = r"\n*#{0,2}\s*name:\n+\s*(\S+)\s*\n*#{0,2}\s*content:\n?(.*)"
    match = re.search(pattern, role_prompt, re.DOTALL)
    if match:
        return match.group(1), match.group(2)
    return None


def try_parse_tool_call_id_and_content(role_prompt):
    # customer can add ## in front of tool_call_id/content for markdown highlight.
    # and we still support tool_call_id/content without ## prefix for backward compatibility.
    pattern = r"\n*#{0,2}\s*tool_call_id:\n+\s*(\S+)\s*\n*#{0,2}\s*content:\n?(.*)"
    match = re.search(pattern, role_prompt, re.DOTALL)
    if match:
        return match.group(1), match.group(2)
    return None


def try_parse_tool_calls(role_prompt):
    # customer can add ## in front of tool_calls for markdown highlight.
    # and we still support tool_calls without ## prefix for backward compatibility.
    pattern = r"\n*#{0,2}\s*tool_calls:\n*\s*(\[.*?\])"
    match = re.search(pattern, role_prompt, re.DOTALL)
    if match:
        return match.group(1)
    return None


def is_tools_chunk(last_message):
    return last_message and "role" in last_message and last_message["role"] == "tool" and "content" not in last_message


def is_assistant_tool_calls_chunk(last_message, chunk):
    return last_message and "role" in last_message and last_message["role"] == "assistant" and "tool_calls" in chunk


def parse_tool_calls_for_assistant(last_message, chunk):
    parsed_result = try_parse_tool_calls(chunk)
    error_msg = "Failed to parse assistant role prompt with tool_calls. Please make sure the prompt follows the format:"
    " 'tool_calls:\\n[{ id: tool_call_id, type: tool_type, function: {name: function_name, arguments: function_args }]'"
    "See more details in https://platform.openai.com/docs/api-reference/chat/create#chat-create-messages"

    if parsed_result is None:
        raise ChatAPIAssistantRoleInvalidFormat(message=error_msg)
    else:
        parsed_array = None
        try:
            parsed_array = eval(parsed_result)
            last_message["tool_calls"] = parsed_array
        except Exception:
            raise ChatAPIAssistantRoleInvalidFormat(message=error_msg)


def parse_tools(last_message, chunk, hash2images, image_detail):
    parsed_result = try_parse_tool_call_id_and_content(chunk)
    if parsed_result is None:
        raise ChatAPIToolRoleInvalidFormat(
            message="Failed to parse tool role prompt. Please make sure the prompt follows the "
            "format: 'tool_call_id:\\ntool_call_id\\ncontent:\\ntool_content'. "
            "'tool_call_id' is required if role is tool, and it should be the tool call that this message is responding"
            " to. See more details in https://platform.openai.com/docs/api-reference/chat/create#chat-create-messages"
        )
    else:
        last_message["tool_call_id"] = parsed_result[0]
        last_message["content"] = to_content_str_or_list(parsed_result[1], hash2images, image_detail)


def parse_chat(chat_str, images: List = None, valid_roles: List[str] = None, image_detail: str = 'auto'):
    if not valid_roles:
        valid_roles = VALID_ROLES

    # openai chat api only supports below roles.
    # customer can add single # in front of role name for markdown highlight.
    # and we still support role name without # prefix for backward compatibility.
    separator = r"(?i)^\s*#?\s*(" + "|".join(valid_roles) + r")\s*:\s*\n"

    images = images or []
    hash2images = {str(x): x for x in images}

    chunks = re.split(separator, chat_str, flags=re.MULTILINE)
    chat_list = []

    for chunk in chunks:
        last_message = chat_list[-1] if len(chat_list) > 0 else None
        if is_tools_chunk(last_message):
            parse_tools(last_message, chunk, hash2images, image_detail)
            continue

        if is_assistant_tool_calls_chunk(last_message, chunk):
            parse_tool_calls_for_assistant(last_message, chunk)
            continue

        if (
            last_message
            and "role" in last_message
            and "content" not in last_message
            and "tool_calls" not in last_message
        ):
            parsed_result = try_parse_name_and_content(chunk)
            if parsed_result is None:
                # "name" is required if the role is "function"
                if last_message["role"] == "function":
                    raise ChatAPIFunctionRoleInvalidFormat(
                        message="Failed to parse function role prompt. Please make sure the prompt follows the "
                                "format: 'name:\\nfunction_name\\ncontent:\\nfunction_content'. "
                                "'name' is required if role is function, and it should be the name of the function "
                                "whose response is in the content. May contain a-z, A-Z, 0-9, and underscores, "
                                "with a maximum length of 64 characters. See more details in "
                                "https://platform.openai.com/docs/api-reference/chat/create#chat/create-name "
                                "or view sample 'How to use functions with chat models' in our gallery.")
                # "name" is optional for other role types.
                else:
                    last_message["content"] = to_content_str_or_list(chunk, hash2images, image_detail)
            else:
                last_message["name"] = parsed_result[0]
                last_message["content"] = to_content_str_or_list(parsed_result[1], hash2images, image_detail)
        else:
            if chunk.strip() == "":
                continue
            # Check if prompt follows chat api message format and has valid role.
            # References: https://platform.openai.com/docs/api-reference/chat/create.
            role = chunk.strip().lower()
            validate_role(role, valid_roles=valid_roles)
            new_message = {"role": role}
            chat_list.append(new_message)
    return chat_list


def to_content_str_or_list(chat_str: str, hash2images: Mapping, image_detail: str):
    chat_str = chat_str.strip()
    chunks = chat_str.split("\n")
    include_image = False
    result = []
    for chunk in chunks:
        if chunk.strip() in hash2images:
            image_message = {}
            image_message["type"] = "image_url"
            image_url = hash2images[chunk.strip()].source_url \
                if hasattr(hash2images[chunk.strip()], "source_url") else None
            if not image_url:
                image_bs64 = hash2images[chunk.strip()].to_base64()
                image_mine_type = hash2images[chunk.strip()]._mime_type
                image_url = f"data:{image_mine_type};base64,{image_bs64}"
            image_message["image_url"] = {
                "url": image_url,
                "detail": image_detail
            }
            result.append(image_message)
            include_image = True
        elif chunk.strip() == "":
            continue
        else:
            result.append({"type": "text", "text": chunk})
    return result if include_image else chat_str


def generate_retry_interval(retry_count: int) -> float:
    min_backoff_in_sec = 3
    max_backoff_in_sec = 60
    retry_interval = min_backoff_in_sec + ((2 ** retry_count) - 1)

    if retry_interval > max_backoff_in_sec:
        retry_interval = max_backoff_in_sec
    return retry_interval


def build_deployment_dict(item) -> Deployment:
    model = item.properties.model
    return Deployment(item.name, model.name, model.version)


def _parse_resource_id(resource_id):
    # Resource id is connection's id in following format:
    # "/subscriptions/{sub}/resourceGroups/{rg}/providers/Microsoft.CognitiveServices/accounts/{account}"
    split_parts = resource_id.split("/")
    if len(split_parts) != 9:
        raise ParseConnectionError(
            f"Connection resourceId format invalid, cur resourceId is {resource_id}."
        )
    sub, rg, account = split_parts[2], split_parts[4], split_parts[-1]

    return sub, rg, account


def _get_credential():
    from azure.identity import DefaultAzureCredential
    from azure.ai.ml._azure_environments import _get_default_cloud_name, EndpointURLS, _get_cloud, AzureEnvironments
    # Support sovereign cloud cases, like mooncake, fairfax.
    cloud_name = _get_default_cloud_name()
    if cloud_name != AzureEnvironments.ENV_DEFAULT:
        cloud = _get_cloud(cloud=cloud_name)
        authority = cloud.get(EndpointURLS.ACTIVE_DIRECTORY_ENDPOINT)
        credential = DefaultAzureCredential(authority=authority, exclude_shared_token_cache_credential=True)
    else:
        credential = DefaultAzureCredential()

    return credential


def list_deployment_connections(connection=""):
    try:
        # Do not support dynamic list if azure packages are not installed.
        from azure.mgmt.cognitiveservices import CognitiveServicesManagementClient
        from promptflow.core._errors import OpenURLFailedUserError
        from promptflow.core._connection_provider._connection_provider import ConnectionProvider
        from promptflow.core._connection_provider._workspace_connection_provider import WorkspaceConnectionProvider
        from promptflow.core._connection_provider._http_connection_provider import HttpConnectionProvider
    except ImportError:
        return None

    try:
        print("00000000")
        credential = _get_credential()
        try:
            connection_provider = ConnectionProvider.get_instance()
            conn_sub, conn_rg, conn_account = "", "", ""
            if isinstance(connection_provider, WorkspaceConnectionProvider) \
                    or isinstance(connection_provider, HttpConnectionProvider):
                print("1111111111")
                print("conn provider type....................", type(connection_provider))
                conn = connection_provider.get(connection)
                print("conn type....................", type(conn))
                resource_id = conn.resource_id
                print("resource_id.........:", resource_id)
                if not resource_id:
                    print("44444444444")
                    return None
                conn_sub, conn_rg, conn_account = _parse_resource_id(resource_id)
        except OpenURLFailedUserError:
            print("22222222222")
            return None
        except ListDeploymentsError as e:
            print("33333333333")
            raise e
        except Exception as e:
            msg = f"Parsing connection with exception: {e}"
            raise ListDeploymentsError(msg=msg) from e

        if conn_sub:
            print("conn_sub.........:", conn_sub)
            client = CognitiveServicesManagementClient(
                credential=credential,
                subscription_id=conn_sub,
            )
            return client.deployments.list(
                resource_group_name=conn_rg,
                account_name=conn_account,
            )
        return None
    except Exception as e:
        if hasattr(e, 'status_code') and e.status_code == 403:
            msg = f"Failed to list deployments due to permission issue: {e}"
            raise ListDeploymentsError(msg=msg) from e
        else:
            msg = f"Failed to list deployments with exception: {e}"
            raise ListDeploymentsError(msg=msg) from e


def refine_extra_fields_not_permitted_error(connection, deployment_name, model):
    tsg = "Please kindly avoid using vision model in LLM tool, " \
          "because vision model cannot work with some chat api parameters. " \
          "You can change to use tool 'Azure OpenAI GPT-4 Turbo with Vision' " \
          "or 'OpenAI GPT-4V' for vision model."
    try:
        if isinstance(connection, AzureOpenAIConnection):
            deployment_collection = list_deployment_connections(connection.name)
            for item in deployment_collection:
                if deployment_name == item.name:
                    if item.properties.model.version in [GPT4V_VERSION]:
                        return tsg
        elif isinstance(connection, OpenAIConnection) and model in ["gpt-4-vision-preview"]:
            return tsg
    except Exception as e:
        print(f"Exception occurs when refine extra fields not permitted error for llm: "
              f"{type(e).__name__}: {str(e)}", file=sys.stderr)

    return None


def is_retriable_api_connection_error(e: APIConnectionError):
    retriable_error_messages = [
        "connection aborted",
        # issue 2296
        "server disconnected without sending a response"
    ]
    for message in retriable_error_messages:
        if message in str(e).lower() or message in str(e.__cause__).lower():
            return True

    return False


# TODO(2971352): revisit this tries=100 when there is any change to the 10min timeout logic
def handle_openai_error(tries: int = 100):
    """
    A decorator function that used to handle OpenAI error.
    OpenAI Error falls into retriable vs non-retriable ones.

    For retriable error, the decorator use below parameters to control its retry activity with exponential backoff:
     `tries` : max times for the function invocation, type is int
     'delay': base delay seconds for exponential delay, type is float
    """

    def decorator(func):
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            for i in range(tries + 1):
                try:
                    return func(*args, **kwargs)
                except (SystemErrorException, UserErrorException) as e:
                    # Throw inner wrapped exception directly
                    raise e
                except (APIStatusError, APIConnectionError) as e:
                    #  Handle retriable exception, please refer to
                    #  https://platform.openai.com/docs/guides/error-codes/api-errors
                    print(f"Exception occurs: {type(e).__name__}: {str(e)}", file=sys.stderr)
                    # Vision model does not support all chat api parameters, e.g. response_format and function_call.
                    # Recommend user to use vision model in vision tools, rather than LLM tool.
                    # Related issue https://github.com/microsoft/promptflow/issues/1683
                    if isinstance(e, BadRequestError) and "extra fields not permitted" in str(e).lower():
                        refined_error_message = \
                            refine_extra_fields_not_permitted_error(args[0].connection,
                                                                    kwargs.get("deployment_name", ""),
                                                                    kwargs.get("model", ""))
                        if refined_error_message:
                            raise LLMError(message=f"{str(e)} {refined_error_message}")
                        else:
                            raise WrappedOpenAIError(e)

                    if isinstance(e, APIConnectionError) and not isinstance(e, APITimeoutError) \
                            and not is_retriable_api_connection_error(e):
                        raise WrappedOpenAIError(e)
                    # Retry InternalServerError(>=500), RateLimitError(429), UnprocessableEntityError(422)
                    if isinstance(e, APIStatusError):
                        status_code = e.response.status_code
                        if status_code < 500 and status_code not in [429, 422]:
                            raise WrappedOpenAIError(e)
                    if isinstance(e, RateLimitError) and getattr(e, "type", None) == "insufficient_quota":
                        # Exit retry if this is quota insufficient error
                        print(f"{type(e).__name__} with insufficient quota. Throw user error.", file=sys.stderr)
                        raise WrappedOpenAIError(e)
                    if i == tries:
                        # Exit retry if max retry reached
                        print(f"{type(e).__name__} reached max retry. Exit retry with user error.", file=sys.stderr)
                        raise ExceedMaxRetryTimes(e)

                    if hasattr(e, 'response') and e.response is not None:
                        retry_after_in_header = e.response.headers.get("retry-after", None)
                    else:
                        retry_after_in_header = None

                    if not retry_after_in_header:
                        retry_after_seconds = generate_retry_interval(i)
                        msg = (
                            f"{type(e).__name__} #{i}, but no Retry-After header, "
                            + f"Back off {retry_after_seconds} seconds for retry."
                        )
                        print(msg, file=sys.stderr)
                    else:
                        retry_after_seconds = float(retry_after_in_header)
                        msg = (
                            f"{type(e).__name__} #{i}, Retry-After={retry_after_in_header}, "
                            f"Back off {retry_after_seconds} seconds for retry."
                        )
                        print(msg, file=sys.stderr)
                    time.sleep(retry_after_seconds)
                except OpenAIError as e:
                    # For other non-retriable errors from OpenAIError,
                    # For example, AuthenticationError, APIConnectionError, BadRequestError, NotFoundError
                    # Mark UserError for all the non-retriable OpenAIError
                    print(f"Exception occurs: {type(e).__name__}: {str(e)}", file=sys.stderr)
                    raise WrappedOpenAIError(e)
                except Exception as e:
                    print(f"Exception occurs: {type(e).__name__}: {str(e)}", file=sys.stderr)
                    error_message = f"OpenAI API hits exception: {type(e).__name__}: {str(e)}"
                    raise LLMError(message=error_message)

        return wrapper

    return decorator


def to_bool(value) -> bool:
    return str(value).lower() == "true"


def render_jinja_template(prompt, trim_blocks=True, keep_trailing_newline=True, **kwargs):
    try:
        return Template(prompt, trim_blocks=trim_blocks, keep_trailing_newline=keep_trailing_newline).render(**kwargs)
    except Exception as e:
        # For exceptions raised by jinja2 module, mark UserError
        print(f"Exception occurs: {type(e).__name__}: {str(e)}", file=sys.stderr)
        error_message = f"Failed to render jinja template: {type(e).__name__}: {str(e)}. " \
                        + "Please modify your prompt to fix the issue."
        raise JinjaTemplateError(message=error_message) from e


def build_escape_dict(kwargs: dict):
    escape_dict = {}
    for _, value in kwargs.items():
        escape_dict = _build_escape_dict(value, escape_dict)
    return escape_dict


def _build_escape_dict(val, escape_dict: dict):
    """
    Build escape dictionary with roles as keys and uuids as values.
    """
    if isinstance(val, ChatInputList):
        for item in val:
            _build_escape_dict(item, escape_dict)
    elif isinstance(val, str):
        pattern = r"(?i)^\s*#?\s*(" + "|".join(VALID_ROLES) + r")\s*:\s*\n"
        roles = re.findall(pattern, val, flags=re.MULTILINE)
        for role in roles:
            if role not in escape_dict:
                # We cannot use a hard-coded hash str for each role, as the same role might be in various case formats.
                # For example, the 'system' role may vary in input as 'system', 'System', 'SysteM','SYSTEM', etc.
                # To convert the escaped roles back to the original str, we need to use different uuids for each case.
                escape_dict[role] = str(uuid.uuid4())

    return escape_dict


def escape_roles(val, escape_dict: dict):
    """
    Escape the roles in the prompt inputs to avoid the input string with pattern '# role' get parsed.
    """
    if isinstance(val, ChatInputList):
        return ChatInputList([escape_roles(item, escape_dict) for item in val])
    elif isinstance(val, str):
        for role, encoded_role in escape_dict.items():
            val = val.replace(role, encoded_role)
        return val
    else:
        return val


def unescape_roles(val, escape_dict: dict):
    """
    Unescape the roles in the parsed chat messages to restore the original role names.

    Besides the case that value is: 'some text. escaped_roles (i.e. fake uuids)'
    We also need to handle the vision case that the content is converted to list.
    For example:
        [{
            'type': 'text',
            'text': 'some text. fake_uuid'
        }, {
            'type': 'image_url',
            'image_url': {}
        }]
    """
    if isinstance(val, str):
        for role, encoded_role in escape_dict.items():
            val = val.replace(encoded_role, role)
        return val
    elif isinstance(val, list):
        for index, item in enumerate(val):
            if isinstance(item, dict) and "text" in item:
                for role, encoded_role in escape_dict.items():
                    val[index]["text"] = item["text"].replace(encoded_role, role)
        return val
    else:
        return val


def build_messages(
    prompt: PromptTemplate,
    images: List = None,
    image_detail: str = 'auto',
    **kwargs,
):
    # Use escape/unescape to avoid unintended parsing of role in user inputs.
    escape_dict = build_escape_dict(kwargs)
    updated_kwargs = {
        key: escape_roles(value, escape_dict) for key, value in kwargs.items()
    }

    # keep_trailing_newline=True is to keep the last \n in the prompt to avoid converting "user:\t\n" to "user:".
    chat_str = render_jinja_template(
        prompt, trim_blocks=True, keep_trailing_newline=True, **updated_kwargs
    )
    messages = parse_chat(chat_str, images=images, image_detail=image_detail)

    if escape_dict and isinstance(messages, list):
        for message in messages:
            if not isinstance(message, dict):
                continue
            for key, val in message.items():
                message[key] = unescape_roles(val, escape_dict)

    return messages


def process_function_call(function_call):
    if function_call is None:
        param = "auto"
    elif function_call == "auto" or function_call == "none":
        param = function_call
    else:
        function_call_example = json.dumps({"name": "function_name"})
        common_tsg = f"Here is a valid example: {function_call_example}. See the guide at " \
                     "https://platform.openai.com/docs/api-reference/chat/create#chat/create-function_call " \
                     "or view sample 'How to call functions with chat models' in our gallery."
        param = function_call
        if not isinstance(param, dict):
            raise ChatAPIInvalidFunctions(
                message=f"function_call parameter '{param}' must be a dict, but not {type(function_call)}. {common_tsg}"
            )
        else:
            if "name" not in function_call:
                raise ChatAPIInvalidFunctions(
                    message=f'function_call parameter {json.dumps(param)} must contain "name" field. {common_tsg}'
                )
    return param


def process_tool_choice(tool_choice):
    if tool_choice is None:
        param = "auto"
    elif tool_choice == "auto" or tool_choice == "none":
        param = tool_choice
    else:
        tool_choice_example = json.dumps({"type": "function", "function": {"name": "my_function"}})
        common_tsg = (
            f"Here is a valid example: {tool_choice_example}. See the guide at "
            "https://platform.openai.com/docs/api-reference/chat/create."
        )
        param = tool_choice
        if not isinstance(param, dict):
            raise ChatAPIInvalidTools(
                message=f"tool_choice parameter '{param}' must be a dict, but not {type(tool_choice)}. {common_tsg}"
            )
        else:
            if "type" not in tool_choice:
                raise ChatAPIInvalidTools(
                    message=f'tool_choice parameter {json.dumps(param)} must contain "type" field. {common_tsg}'
                )

            if "function" not in tool_choice:
                raise ChatAPIInvalidTools(
                    message=f'tool_choice parameter {json.dumps(param)} must contain "function" field. {common_tsg}'
                )

            if not isinstance(param["function"], dict):
                raise ChatAPIInvalidTools(
                    message=f'function parameter "{param["function"]}" in tool_choice must be a dict, '
                            f'but not {type(param["function"])}. {common_tsg}'
                )
            elif "name" not in tool_choice["function"]:
                raise ChatAPIInvalidTools(
                    message=f'function parameter "{json.dumps(param["function"])}" in tool_choice must '
                            f'contain "name" field. {common_tsg}'
                )
    return param


def post_process_chat_api_response(completion, stream, functions=None, tools=None):
    if stream:
        # TODO: test if tools is supported by stream mode.
        if functions is not None:
            error_message = "Function calling has not been supported by stream mode yet."
            raise FunctionCallNotSupportedInStreamMode(message=error_message)

        def generator():
            for chunk in completion:
                if chunk.choices:
                    yield chunk.choices[0].delta.content if hasattr(chunk.choices[0].delta, 'content') and \
                                                            chunk.choices[0].delta.content is not None else ""

        # We must return the generator object, not using yield directly here.
        # Otherwise, the function itself will become a generator, despite whether stream is True or False.
        return generator()
    else:
        # When calling function/tool, function_call/tool_call response will be returned as a field in message,
        # so we need return message directly. Otherwise, we only return content.
        if functions or tools:
            return completion.model_dump()["choices"][0]["message"]
        else:
            # chat api may return message with no content.
            return getattr(completion.choices[0].message, "content", "")


def preprocess_template_string(template_string: str) -> str:
    """Remove the image input decorator from the template string and place the image input in a new line."""
    pattern = re.compile(r'\!\[(\s*image\s*)\]\(\{\{(\s*[^\s{}]+\s*)\}\}\)')

    # Find all matches in the input string
    matches = pattern.findall(template_string)

    # Perform substitutions
    for match in matches:
        original = f"![{match[0]}]({{{{{match[1]}}}}})"
        replacement = f"\n{{{{{match[1]}}}}}\n"
        template_string = template_string.replace(original, replacement)

    return template_string


def convert_to_chat_list(obj):
    if isinstance(obj, dict):
        return {key: convert_to_chat_list(value) for key, value in obj.items()}
    elif isinstance(obj, list):
        return ChatInputList([convert_to_chat_list(item) for item in obj])
    else:
        return obj


def add_referenced_images_to_set(value, image_set, image_type):
    if isinstance(value, image_type):
        image_set.add(value)
    elif isinstance(value, list):
        for item in value:
            add_referenced_images_to_set(item, image_set, image_type)
    elif isinstance(value, dict):
        for _, item in value.items():
            add_referenced_images_to_set(item, image_set, image_type)


def find_referenced_image_set(kwargs: dict):
    referenced_images = set()
    try:
        from promptflow.contracts.multimedia import Image

        for _, value in kwargs.items():
            add_referenced_images_to_set(value, referenced_images, Image)
    except ImportError:
        pass
    return referenced_images


def normalize_connection_config(connection):
    """
    Normalizes the configuration of a given connection object for compatibility.

    This function takes a connection object and normalizes its configuration,
    ensuring it is compatible and standardized for use.
    """
    try:
        from promptflow.connections import ServerlessConnection
    except ImportError:
        # If unable to import ServerlessConnection, define a placeholder class to allow isinstance checks to pass.
        # ServerlessConnection was introduced in pf version 1.6.0.
        class ServerlessConnection:
            pass

    if isinstance(connection, AzureOpenAIConnection):
        if connection.api_key:
            return {
                # disable OpenAI's built-in retry mechanism by using our own retry
                # for better debuggability and real-time status updates.
                "max_retries": 0,
                "api_key": connection.api_key,
                "api_version": connection.api_version,
                "azure_endpoint": connection.api_base,
            }
        else:
            return {
                "max_retries": 0,
                "api_version": connection.api_version,
                "azure_endpoint": connection.api_base,
                "azure_ad_token_provider": connection.get_token,
            }
    elif isinstance(connection, OpenAIConnection):
        return {
            "max_retries": 0,
            "api_key": connection.api_key,
            "organization": connection.organization,
            "base_url": connection.base_url
        }
    elif isinstance(connection, ServerlessConnection):
        suffix = "/v1"
        base_url = connection.api_base
        if not base_url.endswith(suffix):
            # append "/v1" to ServerlessConnection api_base so that it can directly use the OpenAI SDK.
            base_url += suffix
        return {
            "max_retries": 0,
            "api_key": connection.api_key,
            "base_url": base_url
        }
    else:
        error_message = f"Not Support connection type '{type(connection).__name__}'. " \
                        "Connection type should be in [AzureOpenAIConnection, OpenAIConnection, " \
                        "ServerlessConnection]."
        raise InvalidConnectionType(message=error_message)


def init_openai_client(connection: OpenAIConnection):
    try:
        from openai import OpenAI as OpenAIClient
    except ImportError as e:
        if "cannot import name 'OpenAI' from 'openai'" in str(e):
            raise ImportError(
                "Please upgrade your OpenAI package to version 1.0.0 or later" +
                "using the command: pip install --upgrade openai.")
        else:
            raise e

    conn_dict = normalize_connection_config(connection)
    return OpenAIClient(**conn_dict)


def init_azure_openai_client(connection: AzureOpenAIConnection):
    try:
        from openai import AzureOpenAI as AzureOpenAIClient
    except ImportError as e:
        if "cannot import name 'AzureOpenAI' from 'openai'" in str(e):
            raise ImportError(
                "Please upgrade your OpenAI package to version 1.0.0 or later" +
                "using the command: pip install --upgrade openai.")
        else:
            raise e

    conn_dict = normalize_connection_config(connection)
    return AzureOpenAIClient(**conn_dict)
