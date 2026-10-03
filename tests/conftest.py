import os

# FastAPI's test client calls the app as http://testserver; set before runapp.config is imported.
os.environ.setdefault("RUNAPP_ALLOWED_HOSTS", "testserver")
