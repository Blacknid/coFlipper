"""The coFlipper agent: connects the Gemini model to the Flipper Zero device.

The user writes in natural language, the model decides which CFP commands are needed,
the agent executes them on the device and returns the real results to the model, on
whose basis it formulates the final answer.

Running:
    python agent.py           # with a Flipper connected over USB
    python agent.py --mock    # without a device, for development
"""

import argparse
import os
import sys
import time

from dotenv import load_dotenv
from google import genai
from google.genai import errors, types

from commands import CommandDispatcher, build_tool, device_commands, load_catalog

# The model is pinned deliberately, not via a "latest" alias: the alias always tracks the
# most recent generation, and that generation comes with different usage limits. Concretely,
# the gemini-flash-latest alias pointed to gemini-3.6-flash, limited to 20 requests per day
# on the free plan - insufficient for development and demonstration.
# It can be changed without modifying the code, through the COFLIPPER_MODEL env variable.
MODEL = os.environ.get("COFLIPPER_MODEL", "gemini-3.5-flash")

# The API occasionally returns 503 when overloaded. Without a retry, such a transient
# error would interrupt the conversation in progress.
SEND_RETRIES = 3
RETRY_DELAY_S = 2.0

SYSTEM_INSTRUCTION = """You are the assistant of the coFlipper project. You control a
Flipper Zero device connected over USB, using the tools made available to you.

Rules you follow strictly:
1. Any information about the state of the device or about the signals around it comes
   EXCLUSIVELY from the result of a tool call. You never invent frequencies,
   UIDs, protocols, or hardware readings.
2. If a tool responds with an error (for example 'not_implemented'), you tell the user
   openly that the feature in question is not yet implemented on the device.
   You do not compensate for the error with a plausible, invented answer.
3. You may explain general technical concepts from your own knowledge, but you clearly
   mark the difference between a general explanation and data measured by the device.
4. Reply in the language in which you received the prompt.
"""


def build_client_for_device():
    # connect() also launches the CFP application on the device if it is not already
    # open - without it running, the Flipper does not know the `cfp` command at all.
    from cfp_client import connect

    return connect()


def send_with_retry(chat, message):
    for attempt in range(1, SEND_RETRIES + 1):
        try:
            return chat.send_message(message)
        except errors.ServerError as exc:
            if attempt == SEND_RETRIES:
                raise
            print(f"  [gemini] service unavailable ({exc.code}), retrying...")
            time.sleep(RETRY_DELAY_S * attempt)
        except errors.ClientError as exc:
            if exc.code != 429:
                raise
            # A 429 with 'limit: 0' does not mean a quota we consumed ourselves, but a
            # model that is not available at all on the current plan: retrying is pointless.
            if "limit: 0" in str(exc):
                sys.exit(
                    f"The model {MODEL} is not available on this API key's plan.\n"
                    "Choose another one through the COFLIPPER_MODEL environment variable "
                    "(list_models.py shows what exists)."
                )
            if attempt == SEND_RETRIES:
                raise
            print("  [gemini] request limit reached, waiting...")
            time.sleep(RETRY_DELAY_S * attempt * 5)


def run_turn(chat, dispatcher, message):
    """One conversation turn: may include several rounds of tool calls."""
    response = send_with_retry(chat, message)

    while response.function_calls:
        results = []
        for call in response.function_calls:
            print(f"  [flipper] {call.name} {dict(call.args or {})}")
            outcome = dispatcher.dispatch(call.name, call.args)
            print(f"  [flipper] -> {outcome}")
            results.append(
                types.Part.from_function_response(name=call.name, response=outcome)
            )
        response = send_with_retry(chat, results)

    return response.text


def main():
    parser = argparse.ArgumentParser(description="The coFlipper agent (Gemini + Flipper Zero)")
    parser.add_argument(
        "--mock",
        action="store_true",
        help="use a simulated Flipper, without a physical device",
    )
    args = parser.parse_args()

    load_dotenv()
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        sys.exit("GEMINI_API_KEY is not set. Put it in desktop/.env (see .env.example).")

    catalog = load_catalog()
    commands = device_commands(catalog)
    if not commands:
        sys.exit("No command available in commands.json.")

    if args.mock:
        from mock_flipper import MockCFPClient

        flipper = MockCFPClient()
        print("Simulated mode: no physical device is used.")
    else:
        flipper = build_client_for_device()

    dispatcher = CommandDispatcher(commands, flipper)
    genai_client = genai.Client(api_key=api_key)
    chat = genai_client.chats.create(
        model=MODEL,
        config=types.GenerateContentConfig(
            system_instruction=SYSTEM_INSTRUCTION,
            tools=[build_tool(commands)],
        ),
    )

    names = ", ".join(cmd["name"] for cmd in commands)
    print(f"Tools available to the model: {names}")
    print("Write a request in natural language. Ctrl+C to finish.\n")

    try:
        while True:
            message = input("> ").strip()
            if not message:
                continue
            print(run_turn(chat, dispatcher, message))
    except KeyboardInterrupt:
        print("\nSession stopped.")
    finally:
        flipper.close()


if __name__ == "__main__":
    main()
