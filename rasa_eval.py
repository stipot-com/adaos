import json
from pathlib import Path
project = Path(r"C:\Users\jeste\.adaos\state\interpreter\rasa_project")
nlu_md = (project / 'data' / 'intents_from_config.yml').read_text(encoding='utf-8')
print(nlu_md[:500])
