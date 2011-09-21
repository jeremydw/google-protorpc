#!/usr/bin/env python
#
# Copyright 2011 Google Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#

"""ProtoRPC WSGI service applications.

Use functions in this module to configure ProtoRPC services for use with
WSGI applications.  For more information about WSGI, please see:

  http://wsgi.org/wsgi
  http://docs.python.org/library/wsgiref.html
"""

__author__ = 'rafek@google.com (Rafe Kaplan)'

import cgi
import httplib
import logging
import re

from wsgiref import headers as wsgi_headers

from protorpc import protobuf
from protorpc import protojson
from protorpc import messages
from protorpc import remote
from protorpc import util
from protorpc.wsgi import util as wsgi_util

__all__ = [
  'service_app',
]

_METHOD_PATTERN = r'(?:\.([^?]+))'
_REQUEST_PATH_PATTERN = r'^(%%s)%s$' % _METHOD_PATTERN

_HTTP_BAD_REQUEST = wsgi_util.error(httplib.BAD_REQUEST)
_HTTP_NOT_FOUND = wsgi_util.error(httplib.NOT_FOUND)
_HTTP_UNSUPPORTED_MEDIA_TYPE = wsgi_util.error(httplib.UNSUPPORTED_MEDIA_TYPE)


@util.positional(2)
def service_mapping(service_factory, service_path=r'.*'):
  """WSGI application that handles a single ProtoRPC service mapping.

  Args:
    service_factory: Service factory for creating instances of service request
      handlers.  Either callable that takes no parameters and returns a service
      instance or a service class whose constructor requires no parameters.
    service_path: Regular expression for matching requests against.  Requests
      that do not have matching paths will cause a 404 (Not Found) response.
  """
  # Just create default protocols configuration.
  # Includes protobuf and protojson.
  # TODO(rafek): Support user provided protocols configuration.
  protocols = remote.Protocols()
  protocols.add_protocol(protobuf, 'protobuf')
  protocols.add_protocol(protojson, 'protojson')

  service_class = getattr(service_factory, 'service_class', service_factory)
  remote_methods = service_class.all_remote_methods()
  path_matcher = re.compile(_REQUEST_PATH_PATTERN % service_path)  

  def protorpc_service_app(environ, start_response):
    """Actual WSGI application function."""
    path_match = path_matcher.match(environ['PATH_INFO'])
    if not path_match:
      return _HTTP_NOT_FOUND(environ, start_response)
    service_path = path_match.group(1)
    method_name = path_match.group(2)

    content_type = environ.get('CONTENT_TYPE')
    if not content_type:
      content_type = environ.get('HTTP_CONTENT_TYPE')
    if not content_type:
      return _HTTP_BAD_REQUEST(environ, start_response)

    # TODO(rafek): Handle alternate encodings.
    content_type = cgi.parse_header(content_type)[0]

    request_method = environ['REQUEST_METHOD']
    if request_method != 'POST':
      content = ('%s.%s is a ProtoRPC method.\n\n'
                 'Service %s\n\n'
                 'More about ProtoRPC: '
                 '%s\n' %
                 (util.PROTORPC_PROJECT_URL,
                  service_path,
                  method_name,
                  service_class.definition_name().encode('utf-8')))
      error_handler = wsgi_util.error(
        httplib.METHOD_NOT_ALLOWED,
        httplib.responses[httplib.METHOD_NOT_ALLOWED],
        content=content,
        content_type='text/plain; charset=utf-8')
      return error_handler(environ, start_response)

    try:
      protocol = protocols.lookup_by_content_type(content_type)
    except KeyError:
      return _HTTP_UNSUPPORTED_MEDIA_TYPE(environ,start_response)

    def send_rpc_error(status_code, state, message, error_name=None):
      """Helper function to send an RpcStatus message as response.

      Will create static error handler and begin response.

      Args:
        status_code: HTTP integer status code.
        state: remote.RpcState enum value to send as response.
        message: Helpful message to send in response.
        error_name: Error name if applicable.

      Returns:
        List containing encoded content response using the same content-type as
        the request.
      """
      status = remote.RpcStatus(state=state,
                                error_message=message,
                                error_name=error_name)
      encoded_status = protocol.encode_message(status)
      error_handler = wsgi_util.error(
        status_code,
        content_type=protocol.default_content_type,
        content=encoded_status)
      return error_handler(environ, start_response)

    method = remote_methods.get(method_name)
    if not method:
      return send_rpc_error(httplib.BAD_REQUEST,
                            remote.RpcState.METHOD_NOT_FOUND_ERROR,
                            'Unrecognized RPC method: %s' % method_name)

    content_length = int(environ.get('CONTENT_LENGTH', '0'))

    remote_info = method.remote
    try:
      request = protocol.decode_message(
        remote_info.request_type, environ['wsgi.input'].read(content_length))
    except (messages.ValidationError, messages.DecodeError), err:
      return send_rpc_error(httplib.BAD_REQUEST,
                            remote.RpcState.REQUEST_ERROR,
                            'Error parsing ProtoRPC request '
                            '(Unable to parse request content: %s)' % err)

    instance = service_factory()

    initialize_request_state = getattr(
      instance, 'initialize_request_state', None)
    if initialize_request_state:
      # TODO(rafek): This is not currently covered by tests.
      server_port = environ.get('SERVER_PORT', None)
      if server_port:
        server_port = int(server_port)

      headers = []
      for name, value in environ.iteritems():
        if name.startswith('HTTP_'):
          headers.append((name[len('HTTP_'):].lower().replace('_', '-'), value))
      request_state = remote.HttpRequestState(
        remote_host=environ.get('REMOTE_HOST', None),
        remote_address=environ.get('REMOTE_ADDR', None),
        server_host=environ.get('SERVER_HOST', None),
        server_port=server_port,
        http_method=request_method,
        service_path=service_path,
        headers=headers)

      initialize_request_state(request_state)

    try:
      response = method(instance, request)
      encoded_response = protocol.encode_message(response)
    except remote.ApplicationError, err:
      return send_rpc_error(httplib.BAD_REQUEST,
                            remote.RpcState.APPLICATION_ERROR,
                            err.message,
                            err.error_name)
    except Exception, err:
      logging.error('Encountered unexpected error from ProtoRPC '
                    'method implementation: %s (%s)' % (err.__class__.__name__,
                                                        err))
      return send_rpc_error(httplib.INTERNAL_SERVER_ERROR,
                            remote.RpcState.SERVER_ERROR,
                            'Internal Server Error')

    response_headers = [('content-type', content_type)]
    start_response('%d %s' % (httplib.OK, httplib.responses[httplib.OK],),
                   response_headers)
    return [encoded_response]

  # Return WSGI application.
  return protorpc_service_app
