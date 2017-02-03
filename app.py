import os
from bihan import application

with application.register:
    import home

application.run(host=os.environ['HOSTNAME'], port=8080)