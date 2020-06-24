from flask import Flask
from werkzeug.middleware.dispatcher import DispatcherMiddleware
from redfish_exporter import make_wsgi_app

# Create my app
app = Flask(__name__)

@app.route("/health")
def health():
    return "OK"

# Add prometheus wsgi middleware to route /metrics requests
app_dispatch = DispatcherMiddleware(app, {
    '/metrics': make_wsgi_app()
})
