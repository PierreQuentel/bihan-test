from bihan import application

with application.register:
    import home

application.run(port=8080)