from bihan import application

with application.register:
    import home

if __name__ == '__main__':
    application.run(port=8080)