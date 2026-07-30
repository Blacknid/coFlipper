import os
import sys

from dotenv import load_dotenv
from google import genai

load_dotenv()

api_key = os.environ.get("GEMINI_API_KEY")
if not api_key:
    sys.exit("GEMINI_API_KEY is not set. Put it in desktop/.env (see .env.example).")

client = genai.Client(api_key=api_key)

try:
    while True:
        message = input("> ")
        if not message.strip():
            continue
        response = client.models.generate_content(
            model="gemini-flash-latest",
            contents=message,
        )
        print(response.text)
except KeyboardInterrupt:
    print("\nSession stopped.")
