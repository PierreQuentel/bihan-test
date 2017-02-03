import os
from bihan import application

with application.register:
    import home

application.run(host=os.environ.get('HOSTNAME', 'localhost'), port=8080,
    debug=False)
