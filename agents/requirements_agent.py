import json

from requirements_agent import parse_args, run


if __name__ == "__main__":
    print(json.dumps(run(parse_args()), indent=2))
