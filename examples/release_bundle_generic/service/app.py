from fastapi import FastAPI

app = FastAPI(title="example-service")

@app.get("/health")
def health():
    return {"status": "ok", "service": "example-service"}
