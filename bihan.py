"""Minimalist Python web server engine.
Documentation at https://github.com/PierreQuentel/bihan
"""

import sys
import os
import imp
import importlib
import re
import io
import traceback
import datetime
import cgi
import urllib.parse
import http.cookies
import http.server
import email.utils
import email.message
import json


class HttpRedirection(Exception):pass
class HttpError(Exception):pass
class DispatchError(Exception): pass
class RoutingError(Exception): pass


class Message:

    def __init__(self):
        self.headers = email.message.Message()
        self.cookies = http.cookies.SimpleCookie()


class Dialog:
    """Instances of Dialog are passed as arguments to the script functions.
    They have attributes taken from the application instance."""

    def __init__(self, obj):
        self.request = obj.request
        self.response = obj.response
        self.root = obj.root
        self.environ = obj.env
        self.template = obj.template
        self.redirection = HttpRedirection
        self.error = HttpError


class application(http.server.SimpleHTTPRequestHandler):

    debug = True
    modules = []
    root = os.getcwd()
    patterns = {}

    def __init__(self, environ, start_response):
    
        self.env = environ
        self.start_response = start_response

        # Set attributes for logging
        path = self.env["PATH_INFO"]
        if self.env["QUERY_STRING"]:
            path += "?"+self.env["QUERY_STRING"]
        
        self.request_version = self.env["SERVER_PROTOCOL"]
        self.requestline = "{} {} {}".format(self.env["REQUEST_METHOD"],
            path, self.request_version)
        self.client_address = [self.env["REMOTE_ADDR"],
            self.env.get("REMOTE_PORT", self.env["SERVER_PORT"])]

        # Initialise attribute "request" from data sent by user agent
        self.request = request = Message()
        request.url = self.env["PATH_INFO"]
        request.method = self.env["REQUEST_METHOD"]
        
        for key in self.env:
            if key=="HTTP_COOKIE":
                request.cookies = http.cookies.SimpleCookie(self.env[key])
            elif key.startswith("HTTP_"):
                request.headers[key[5:]] = self.env[key]
            elif key.upper() == "CONTENT_LENGTH":
                request.headers["Content-Length"] = self.env[key]
            elif key.upper() == "CONTENT_TYPE":
                request.headers["Content-Type"] = self.env[key]

        # Initialise attribute "response"
        self.response = Message()
        self.response.encoding = "utf-8"

        self.status = "200 Ok"

    def __iter__(self):
        """Iteration expected by the WSGI protocol. Calls start_response
        then yields the response body
        """
        try:
            self.get_request_fields()
            self.handle()
        except:
            out = io.StringIO()
            traceback.print_exc(file=out)
            self.response.headers.set_type("text/plain")
            self.response.body = out.getvalue().encode(self.response.encoding)

        self.start_response(str(self.status), self.response_headers())
        yield self.response.body

    class Register:
        
        def __enter__(self):
            """Store list of imported modules when entering the "with" block
            """
            self.modules = list(sys.modules)
            with open('trace.txt', 'a', encoding='utf-8') as out:
                out.write('enter Register\n')
        
        def __exit__(self, exc_type, exc_val, exc_tb):
            """Store the modules that will be used to serve urls"""
            application.modules = [mod for name, mod in sys.modules.items() 
                if not name in self.modules
                and hasattr(mod, "__file__")
                and mod.__file__.startswith(os.getcwd())
                and not getattr(mod, "__exclude__", False)
            ]
            # run load_routes to check if there are duplicate urls
            with open('trace.txt', 'a', encoding='utf-8') as out:
                out.write('exit Register\n')
                out.write(str(application.modules)+'\n')

    register = Register()

    def get_request_fields(self):
        """Set self.request.fields, a dictionary indexed by field names
        If field name ends with [], the value is a list of values
        Else, it is a single value, or a list if there are several values
        """
        request = self.request
        request.fields = {}

        # Get request fields from query string
        fields = cgi.parse_qs(self.env.get("QUERY_STRING", ""), 
            keep_blank_values=1)
        
        for key in fields:
            if key.endswith("[]"):
                request.fields[key[:-2]] = fields[key]
            elif len(fields[key]) == 1:
                request.fields[key] = fields[key][0]
            else:
                request.fields[key] = fields[key]

        if request.method in ["POST", "PUT", "DELETE"]:

            # Get encoding of request data
            charset = "utf-8"
            for key in request.headers:
                mo = re.search("charset\s*=(.*)$", request.headers[key])
                if mo:
                    charset = mo.groups()[0]
                    break
            request.encoding = charset

            fp = self.env["wsgi.input"]

            has_keys = True
            if "Content-Type" in request.headers:
                ctype, pdict = cgi.parse_header(request.headers["Content-Type"])
                has_keys = ctype == "application/x-www-form-urlencoded" or \
                    ctype.startswith("multipart/")

            # If data is not structured with key and value (eg JSON content),
            # only read raw data and set attribute "raw" and "json" of request 
            # object
            if not has_keys:
                length = int(request.headers["Content-Length"])
                request.raw = fp.read(length)
                def _json():
                    return json.loads(request.raw.decode(charset))
                request.json = _json
                return

            # Update request fields from POST data
            body = cgi.FieldStorage(fp, headers=request.headers,
                environ={"REQUEST_METHOD": "POST"})

            data = {}
            for k in body.keys():
                if isinstance(body[k],list): # several fields with same name
                    values = [x.value for x in body[k]]
                    if k.endswith('[]'):
                        data[k[:-2]] = values
                    else:
                        data[k] = values
                else:
                    if body[k].filename: # file upload : don't read the value
                        data[k] = body[k]
                    else:
                        if k.endswith('[]'):
                            data[k[:-2]] = [body[k].value]
                        else:
                            data[k] = body[k].value
            request.fields.update(data)
            
    def handle(self):
        """Process the data received"""
        response = self.response
        self.elts = urllib.parse.urlparse(self.env["PATH_INFO"]+
            "?"+self.env["QUERY_STRING"])
        self.url = self.elts[2]
        response.headers.add_header("Content-Type", "text/html") # default

        kind, arg = self.resolve(self.url)
        if kind=='file':
            if not os.path.exists(arg):
                return self.send_error(404, "File not found", 
                    "No file matching {}".format(self.url))
            return self.send_static(arg)
        
        func, kw = arg
        self.request.fields.update(kw)

        # Run function
        return self.render(func)

    def send_static(self, fs_path):
        """Send the content of a file"""
        try:
            f = open(fs_path,'rb')
        except IOError:
            return self.send_error(404, "File not found",
                "No file found for given url")
        # Use browser cache if possible
        if "If-Modified-Since" in self.request.headers:
            ims = email.utils.parsedate(
                self.request.headers["If-Modified-Since"])
            if ims is not None:
                ims_datetime = datetime.datetime(*ims[:7])
                ims_dtstring = ims_datetime.strftime("%d %b %Y %H:%M:%S")
                last_modif = datetime.datetime.utcfromtimestamp(
                    os.stat(fs_path).st_mtime).strftime("%d %b %Y %H:%M:%S")
                if last_modif == ims_dtstring:
                    self.done(304, io.BytesIO())
                    return
        ctype = self.guess_type(fs_path)
        if ctype.startswith("text/"):
            ctype += ";charset=utf-8"
        self.response.headers.set_type(ctype)
        self.response.headers["Content-Length"] = str(os.fstat(f.fileno())[6])
        self.done(200, f)

    @classmethod
    def load_routes(cls):
        """Returns a mapping between regular expressions and paths to 
        scripts and callables
        """
        # on debug mode, reload all modules in application folders
        with open('trace.txt', 'a', encoding='utf-8') as out:
            out.write('enter load_routes, debug {}, modules {}\n'.format(
                str(cls.debug), str(cls.modules)))
        
        if cls.debug:
            for name, module in sys.modules.items():
                if name == "__main__":
                    continue
                filename = getattr(module, "__file__", "")
                if filename.startswith(cls.root):
                    try:
                        imp.reload(module) # deprecated in version 3.4
                    except AttributeError:
                        importlib.reload(module)

        mapping = {}
        for module in cls.modules:
            prefix = ""
            if hasattr(module, "__prefix__"):
                prefix = "/"+module.__prefix__.lstrip("/")
            for key in dir(module):
                obj = getattr(module, key)
                if callable(obj) and not key.startswith("_"):
                    url = obj.url if hasattr(obj, "url") else "/"+key
                    url = prefix + url
                    pattern = re.sub('<(.*?)>', r'(?P<\1>[^/]+?)', url)
                    pattern = "^" + pattern +"$"
                    if pattern in mapping:
                        msg = 'duplicate url "{}":' +"\n - in {} line {}" * 2
                        obj2 = mapping[pattern]
                        raise RoutingError(msg.format(url, 
                            obj2.__code__.co_filename, 
                            obj2.__code__.co_firstlineno,
                            obj.__code__.co_filename,
                            obj.__code__.co_firstlineno))
                    mapping[pattern] = obj
        return mapping

    def resolve(self, url):
        """If url matches a route defined for the application, return the
        tuple ('func', (function_object, arguments)) where function_object is 
        the function to call and arguments is a dictionary for patterns such 
        as url/<arg>.
        Otherwise return the tuple ('file', path) where path is built from the
        application root and the parts in url.
        """
        # Split url in elements separated by /
        elts = urllib.parse.unquote(url).lstrip("/").split("/")

        target, patterns = None, []
        for pattern, obj in application.load_routes().items():
            mo = re.match(pattern, url, flags=re.I)
            if mo:
                patterns.append(pattern)
                if target is not None:
                    # exception if more than one pattern matches the url
                    msg = "url {} matches at least 2 patterns : {}"
                    raise DispatchError(msg.format(url, patterns))
                target = (obj, mo.groupdict())
        if target is not None:
            return 'func', target

        # finally, try a path in the file system
        return 'file', os.path.join(self.root, *elts)

    def render(self, func):
        """Run the function and send its result
        """
        try:
            # run function with Dialog(self) as positional argument
            result = func(Dialog(self))
        except HttpRedirection as url:
            self.response.headers["Location"] = url
            return self.done(302, io.BytesIO())
        except HttpError as err:
            return self.done(err.args[0], io.BytesIO())
        except: # Other exception : print traceback
            result = io.StringIO()
            traceback.print_exc(file=result)
            result = result.getvalue() # string
            return self.send_error(500, "Server error", result)

        # Get response encoding
        encoding = self.response.encoding
        if not "charset" in self.response.headers["Content-Type"]:
            if encoding is not None:
                ctype = self.response.headers["Content-Type"]
                self.response.headers.replace_header("Content-Type",
                    ctype + "; charset={}".format(encoding))

        # Build response body as a bytes stream
        output = io.BytesIO()
        
        if self.request.method != "HEAD":
            if isinstance(result, bytes):
                output.write(result)
            elif isinstance(result, str):
                try:
                    output.write(result.encode(encoding))
                except UnicodeEncodeError:
                    msg = io.StringIO()
                    traceback.print_exc(file=msg)
                    return self.done(500,
                        io.BytesIO(msg.getvalue().encode("ascii")))
            else:
                output.write(str(result).encode(encoding))

        response_code = getattr(self.response, "status", 200)
        self.response.headers["Content-Length"] = output.tell()
        self.done(response_code, output)

    def template(self, filename, **kw):
        """If the template engine patrom is installed, use it to render the
        template file with the specified key/values
        """
        from patrom import TemplateParser, TemplateError
        parser = TemplateParser()
        path = os.path.join(application.root, "templates", filename)
        try:
            result = parser.render(path, **kw)
            self.response.headers.set_type("text/html")
        except TemplateError as exc:
            result = str(exc)
            self.response.headers.set_type("text/plain")
        return result

    def send_error(self, code, expl, msg=""):
        self.status = "{} {}".format(code, expl)
        self.response.headers.set_type("text/plain")
        self.response.body = msg.encode(self.response.encoding)

    def response_headers(self):
        headers = [(k, str(v)) for (k, v) in self.response.headers.items()]
        for morsel in self.response.cookies.values():
            headers.append(("Set-Cookie", morsel.output(header="").lstrip()))
        return headers

    def done(self, code, infile):
        """Send response, cookies, response headers and the data read from 
        infile
        """
        self.status = "{} {}".format(code, 
            http.server.BaseHTTPRequestHandler.responses[code])
        if code == 500:
            self.response.headers.set_type("text/plain")
        infile.seek(0)
        self.response.body = infile.read()

    @classmethod
    def run(cls, host="localhost", port=8000, debug=True):
        application.debug = debug
        from wsgiref.simple_server import make_server
        httpd = make_server(host, port, application)
        print("Serving on port {}".format(port))
        httpd.serve_forever()

if __name__ == '__main__':
    application.run(port=8000)