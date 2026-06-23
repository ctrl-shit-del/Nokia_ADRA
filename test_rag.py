from requirements_agent import extract_requirements
import json

req = extract_requirements(source="test", source_doc_sha="fc3414253cf486baedfa151201149c2f7f07288b5cc8f95ad2605a2b07da12c1")
print(json.dumps(req, indent=2))
