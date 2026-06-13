import json

from packages_agent import run


if __name__ == "__main__":
    print(json.dumps(run(), indent=2))
