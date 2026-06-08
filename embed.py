import pathlib
import re
 
ROOT     = pathlib.Path(__file__).parent
HTML_PATH = ROOT / "public" / "index.html"
CHAT_PATH = ROOT / "api" / "chat.py"
 
def embed():
    html = HTML_PATH.read_text(encoding="utf-8")
    chat = CHAT_PATH.read_text(encoding="utf-8")
 
    # Replace everything between HTML = """..."""
    pattern = r'(HTML\s*=\s*""").*?(""")'
    replacement = f'HTML = """{html}"""'
 
    new_chat, count = re.subn(pattern, replacement, chat, count=1, flags=re.DOTALL)
 
    if count == 0:
        print("ERROR: Could not find HTML = \"\"\"...\"\"\" in api/chat.py")
        print("Make sure chat.py contains a line like:  HTML = \"\"\"...\"\"\" ")
        return
 
    CHAT_PATH.write_text(new_chat, encoding="utf-8")
 
if __name__ == "__main__":
    embed()
