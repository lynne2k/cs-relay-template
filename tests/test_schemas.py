import json
from pathlib import Path
import unittest

try:
    from jsonschema import Draft202012Validator
except ImportError:
    Draft202012Validator = None

ROOT = Path(__file__).resolve().parents[1]


@unittest.skipIf(Draft202012Validator is None, "install the dev extra for schema validation")
class SchemaTests(unittest.TestCase):
    def test_shipped_examples_match_schemas(self):
        for schema_name, example in (("job.v1.json", "templates/job.json"),
                                     ("chain-plan.v1.json", "examples/chain-plan.json")):
            with self.subTest(example=example):
                schema = json.loads((ROOT / "schemas" / schema_name).read_text())
                Draft202012Validator.check_schema(schema)
                Draft202012Validator(schema).validate(json.loads((ROOT / example).read_text()))


if __name__ == "__main__":
    unittest.main()
