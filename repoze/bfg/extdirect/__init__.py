from collections import defaultdict
import json
import traceback

from pyramid.security import has_permission
from pyramid.view import render_view_to_response
from webob import Response
from zope.interface import implements
from zope.interface import Interface
import venusian


# form parameters sent by ExtDirect when using a form-submit
# see http://www.sencha.com/products/js/direct.php
FORM_DATA_KEYS = frozenset([
    "extAction",
    "extMethod",
    "extTID",
    "extUpload",
    "extType"
])

# response to a file upload cannot be return as application/json, ExtDirect
# defines a special html response body for this use case where the response
# data is added to a textarea for faster JS-side decoding (since etxtarea text
# is not a DOM node)
FORM_SUBMIT_RESPONSE_TPL = '<html><body><textarea>%s</textarea></body></html>'


def _mk_cb_key(action_name, method_name):
    return action_name + '#' + method_name


class JsonReprEncoder(json.JSONEncoder):
    """ a convenience wrapper for classes that support json_repr() """
    def default(self, obj):
        jr = getattr(obj, 'json_repr', None)
        if jr is None:
            return super(JsonReprEncoder, self).default(obj)
        return jr()


class IExtdirect(Interface):
    """ marker iface for Extdirect utility """
    pass


class Extdirect(object):
    """
    Handles ExtDirect API respresentation and routing.

    The Extdirect accepts a number of arguments: ``app``,
    ``api_path``, ``router_path``, ``namespace``, ``descriptor``
    and ``expose_exceptions``.

    The ``app`` argument is a python package or module used
    which is used to scan for ``extdirect_method`` decorated
    functions/methods once the API is built.

    The ``api_path`` and ``router_path`` arguments are the
    paths/URIs of repoze.bfg views. ``api_path`` renders
    the ExtDirect API, ``router_path`` is the routing endpoint
    for ExtDirect calls.

    If the ``namespace`` argument is passed it will be used in
    the API as Ext namespace (default is 'Ext.app').

    If the ``descriptor`` argument is passed it's used as ExtDirect
    API descriptor name (default is Ext.app.REMOTING_API).

    See http://www.sencha.com/products/js/direct.php for further infos.

    The optional ``expose_exceptions`` argument controls the output of
    an ExtDirect call - if ``True``, the router will provide additional
    information about exceptions.
    """

    implements(IExtdirect)

    def __init__(self, app, api_path, router_path, namespace='Ext.app',
                 descriptor='Ext.app.REMOTING_API', expose_exceptions=True):
        self.app = app
        self.api_path = api_path
        self.router_path = router_path
        self.namespace = namespace
        self.descriptor = descriptor
        self.expose_exceptions = expose_exceptions

        self.actions = defaultdict(dict)

    def add_action(self, action_name, **settings):
        """
        Registers an action.

        ``action_name``: Action name

        Possible values of `settings``:

        ``method_name``: Method name
        ``callback``: The callback to execute upon client request
        ``numargs``: Number of arguments passed to the wrapped callable
        ``accept_files``: If true, this action will be declared as formHandler in API
        ``permission``: The permission needed to execute the wrapped callable
        ``request_as_last_param``: If true, the wrapped callable will receive a request object
            as last argument

        """
        callback_key = _mk_cb_key(action_name, settings['method_name'])
        self.actions[action_name][callback_key] = settings

    def get_actions(self):
        """ Builds and returns a dict of actions to be used in ExtDirect API """
        ret = {}
        for (k, v) in self.actions.items():
            items = []
            for settings in v.values():
                d = dict(
                    len = settings['numargs'],
                    name = settings['method_name']
                )
                if settings['accepts_files']:
                    d['formHandler'] = True
                items.append(d)
            ret[k] = items
        return ret

    def get_method(self, action, method):
        """ Returns a method's settings """
        if action not in self.actions:
            raise KeyError("Invalid action: " + action)
        key = _mk_cb_key(action, method)
        if key not in self.actions[action]:
            raise KeyError("No such method in '%s': '%s':" % (action, method))
        return self.actions[action][key]

    def dump_api(self, request):
        """ Dumps all known remote methods """
        ret = ["Ext.ns('%s'); %s = " % (self.namespace, self.descriptor)]
        apidict = dict(
            url = request.application_url + '/' + self.router_path,
            type = 'remoting',
            namespace = self.namespace,
            actions = self.get_actions()
        )
        ret.append(json.dumps(apidict))
        ret.append(";")
        return "".join(ret)

    def _do_route(self, action_name, method_name, params, trans_id, request):
        """ Performs routing, i.e. calls decorated methods/functions """
        settings = self.get_method(action_name, method_name)
        permission = settings.get('permission', None)
        ret = {
            "type": "rpc",
            "tid": trans_id,
            "action": action_name,
            "method": method_name,
            "result": None
        }

        req_as_last = settings.get('request_as_last_param', False)
        if params is None:
            params = list()
        if req_as_last:
            params.append(request)

        try:
            callback = settings['callback']
            if hasattr(callback, "im_class"):
                instance = callback.im_class()
                if (permission is not None) \
                        and not has_permission(permission, instance, request):
                    raise Exception("Access denied")
                params.insert(0, instance)
            try:
                ret["result"] = callback(*params)
            except TypeError:
                raise Exception("Invalid method '%s' for action '%s'"
                                % (method_name, action_name,))
        except Exception, e:
            # Let a user defined view for specific exception prevent returning
            # a server error.
            exception_view = render_view_to_response(e, request)
            if exception_view is not None:
                ret["result"] = exception_view
                return ret

            ret["type"] = "exception"
            if self.expose_exceptions:
                ret["result"] = {
                    'error': True,
                    'message': str(e),
                    'exception_class': str(e.__class__),
                    'stacktrace': traceback.format_exc()
                }
            else:
                message = 'Error executing %s.%s' % (action_name, method_name)
                ret["result"] = {
                    'error': True,
                    'message': message
                }
        return ret

    def route(self, request):
        is_form_data = is_form_submit(request)
        if is_form_data:
            params = parse_extdirect_form_submit(request)
        else:
            params = parse_extdirect_request(request)
        ret = []
        for (act, meth, params, tid) in params:
            ret.append(self._do_route(act, meth, params, tid, request))
        if not is_form_data:
            if len(ret) == 1:
                ret = ret[0]
            return (json.dumps(ret, cls=JsonReprEncoder), False)
        ret = ret[0] # form data cannot be batched
        s = json.dumps(ret, cls=JsonReprEncoder).replace('&quot;', '\\&quot;')
        return (FORM_SUBMIT_RESPONSE_TPL % (s,), True)


class extdirect_method(object):
    """ Enables direct extjs access to python methods through json/form submit """

    def __init__(self, action=None, method_name=None, permission=None, accepts_files=False, request_as_last_param=False):
        self._settings = dict(
            action = action,
            method_name = method_name,
            permission = permission,
            accepts_files = accepts_files,
            request_as_last_param = request_as_last_param,
            original_name = None,
        )

    def __call__(self, wrapped):
        original_name = wrapped.func_name
        self._settings["original_name"] = original_name
        if self._settings["method_name"] is None:
            self._settings["method_name"] = original_name

        self.info = venusian.attach(wrapped,
                                    self.register,
                                    category='extdirect')
        self.wrapped = wrapped
        return wrapped

    def _get_settings(self):
        return self._settings.copy()

    def register(self, scanner, name, ob):
        settings = self._get_settings()

        class_context = isinstance(ob, type)

        if class_context:
            callback = getattr(ob, settings["original_name"])
            numargs = callback.im_func.func_code.co_argcount
            # instance var doesn't count
            numargs -= 1
        else:
            callback = ob
            numargs = callback.func_code.co_argcount

        if numargs and settings['request_as_last_param']:
            numargs -= 1

        settings['numargs'] = numargs

        action = settings.pop("action", None)
        if action is not None:
            name = action

        if class_context:
            class_settings = getattr(ob, '__extdirect_settings__', None)
            if class_settings:
                name = class_settings.get("default_action_name", name)
                if settings.get("permission") is None:
                    permission = class_settings.get("default_permission")
                    settings["permission"] = permission

        extdirect = scanner.config.registry.getUtility(IExtdirect)
        extdirect.add_action(name, callback=callback, **settings)


def is_form_submit(request):
    """ Checks if a request contains extdirect form submit """
    left_over = FORM_DATA_KEYS - set(request.params)
    return not left_over


def parse_extdirect_form_submit(request):
    """
        Extracts extdirect remoting parameters from request
        which are provided by a form submission
    """
    params = request.params
    action = params.pop('extAction')
    method = params.pop('extMethod')
    tid = params.pop('extTID')
    # unused
    upload = params.pop('extUpload')
    type_ = params.pop('extType')
    return [(action, method, [data], tid)]


def parse_extdirect_request(request):
    """
        Extracts extdirect remoting parameters from request
        which are provided by an AJAX request
    """
    body = request.body
    decoded_body = json.loads(body)
    ret = []
    if not isinstance(decoded_body, list):
        decoded_body = [decoded_body]
    for p in decoded_body:
        action = p['action']
        method = p['method']
        data = p['data']
        tid = p['tid']
        ret.append((action, method, data, tid))
    return ret


def api_view(request):
    """ Renders the API """
    util = request.registry.getUtility(IExtdirect)
    body = util.dump_api(request)
    return Response(body, content_type='text/javascript', charset='UTF-8')


def router_view(request):
    """ Renders the result of a ExtDirect call """
    util = request.registry.getUtility(IExtdirect)
    (body, is_form_data) = util.route(request)
    ctype = 'application/json'
    if is_form_data:
        ctype = 'text/html'
    return Response(body, content_type=ctype, charset='UTF-8')

