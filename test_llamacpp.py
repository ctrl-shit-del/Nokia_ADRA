import requests
import json
payload = {
    "model": "test",
    "messages": [{"role": "user", "content": "Hello"}],
    "temperature": 0.2
}
try:
    response = requests.post("http://localhost:8080/v1/chat/completions", json=payload, timeout=30)
    data = response.json()
    print("KEYS:", data.keys())
    print("USAGE:", data.get("usage"))
except Exception as e:
    print(e)
