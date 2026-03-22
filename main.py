# FastAPI is used to create the API server
from fastapi import FastAPI, HTTPException

# Middleware to allow requests from frontend (browser, React, etc.)
from fastapi.middleware.cors import CORSMiddleware

# Pydantic is used to define request/response schemas
from pydantic import BaseModel

# Standard libraries
import json
import requests
import os
import re

# Groq client to call LLM models
from groq import Groq

# DuckDuckGo search library
from ddgs import DDGS

# Load environment variables from .env file
from dotenv import load_dotenv


# Load environment variables
load_dotenv()


# Create FastAPI application
app = FastAPI(title="Tool Calling API", version="2.0.0")


# Allow requests from any origin (for development)
# In production you should restrict origins
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# Read API keys from environment variables
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
OW_API_KEY = os.getenv("OW_API_KEY")  # OpenWeatherMap key


# Initialize Groq client
client = Groq(api_key=GROQ_API_KEY)


# ------------------------------------------------
# TOOL DEFINITIONS
# ------------------------------------------------
# This list describes tools available to the model
TOOLS = [
    {
        "name": "get_weather",
        "description": "Get current weather for a city",
        "parameters": {
            "type": "object",
            "properties": {
                "city": {"type": "string", "description": "City name"}
            },
            "required": ["city"]
        }
    },
    {
        "name": "search_web",
        "description": "Search the internet for information",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search query"}
            },
            "required": ["query"]
        }
    }
]


# ------------------------------------------------
# TOOL IMPLEMENTATIONS
# ------------------------------------------------

def get_weather(city: str) -> str:
    """
    Fetch weather information from OpenWeatherMap API
    """

    # If API key is missing return safe message
    if not OW_API_KEY:
        return "Weather API key not configured."

    url = "http://api.openweathermap.org/data/2.5/weather"

    # Send HTTP request
    response = requests.get(url, params={
        "q": city,
        "appid": OW_API_KEY,
        "units": "metric",
        "lang": "en"
    })

    # Handle API errors
    if response.status_code != 200:
        return f"City not found: {city}"

    data = response.json()

    # Extract useful information
    temp = data["main"]["temp"]
    desc = data["weather"][0]["description"]
    humidity = data["main"]["humidity"]

    return f"Weather in {city}: {desc}, {temp}°C, Humidity {humidity}%"


def search_web(query: str) -> str:
    """
    Perform a web search using DuckDuckGo
    """

    try:
        with DDGS() as ddgs:
            results = list(ddgs.text(query, max_results=5))

        if not results:
            return "No results found"

        # Convert search results into readable text
        texts = [f"{r['title']}: {r['body']}" for r in results]

        return "\n".join(texts)

    except Exception as e:
        return f"Search error: {str(e)}"


# ------------------------------------------------
# TOOL EXECUTOR
# ------------------------------------------------

def run_tool(tool_name: str, params: dict) -> str:
    """
    Execute the selected tool
    """

    if tool_name == "get_weather":
        return get_weather(params.get("city", ""))

    elif tool_name == "search_web":
        return search_web(params.get("query", ""))

    return "Tool not found"


# ------------------------------------------------
# JSON EXTRACTION (IMPORTANT FOR SMALL LLMs)
# ------------------------------------------------

def extract_json(text: str):
    """
    Extract JSON from model output.
    Small models often add extra text, so we isolate JSON safely.
    """

    match = re.search(r"\{.*\}", text, re.DOTALL)

    if not match:
        return None

    try:
        return json.loads(match.group())
    except:
        return None


def is_valid_tool_call(data):
    """
    Validate that model output matches tool call schema
    """

    if not isinstance(data, dict):
        return False

    if "tool" not in data:
        return False

    if "params" not in data:
        return False

    return True


# ------------------------------------------------
# REQUEST / RESPONSE MODELS
# ------------------------------------------------

class ChatRequest(BaseModel):
    message: str
    history: list = []


class ChatResponse(BaseModel):
    answer: str
    tool_used: str | None = None
    tool_result: str | None = None


# ------------------------------------------------
# SYSTEM PROMPT
# ------------------------------------------------

def build_system_prompt():
    """
    Build system prompt with tool instructions
    """

    tools_json = json.dumps(TOOLS, indent=2)

    return f"""
You are an AI assistant with access to tools.

TOOLS:
{tools_json}

RULES:

1. If the question requires internet information, use search_web.
2. If the question is about weather, use get_weather.

IMPORTANT RULES:

- Never output reasoning.
- Never output thoughts.
- Never explain when calling a tool.

When calling a tool respond ONLY with JSON in this format:

{{"tool":"tool_name","params":{{"param":"value"}}}}

If no tool is required answer normally.
"""

def clean_response(text: str) -> str:
    """
    Remove chain-of-thought reasoning from model output.
    """
    lines = text.split("\n")

    filtered = []

    for line in lines:

        lower = line.lower()

        if lower.startswith("okay"):
            continue
        if "let me think" in lower:
            continue
        if "the user is asking" in lower:
            continue
        if "i should" in lower:
            continue

        filtered.append(line)

    return "\n".join(filtered).strip()

# ------------------------------------------------
# MAIN CHAT LOGIC
# ------------------------------------------------

async def chat_with_tools(user_message: str, history: list) -> ChatResponse:

    # Initialize message list
    messages = [{"role": "system", "content": build_system_prompt()}]

    # Add previous conversation history (limit to last 6)
    for h in history[-6:]:
        messages.append(h)

    # Add user message
    messages.append({"role": "user", "content": user_message})

    # Call LLM
    response = client.chat.completions.create(
        model="qwen/qwen3-32b",
        messages=messages,
        max_tokens=500,
        temperature=0.1
    )

    # Extract model reply
    # model_reply = response.choices[0].message.content.strip()
    model_reply = clean_response(
    response.choices[0].message.content.strip()
)

    # Try extracting JSON tool call
    tool_call = extract_json(model_reply)

    # If tool call is valid
    if tool_call and is_valid_tool_call(tool_call):

        tool_name = tool_call["tool"]
        params = tool_call["params"]

        # Run the tool
        tool_result = run_tool(tool_name, params)

        # Add tool result to conversation
        messages.append({"role": "assistant", "content": model_reply})

        messages.append({
            "role": "user",
            "content": f"Tool Result:\n{tool_result}\n\nProvide the final answer."
        })

        # Ask model to generate final response
        final_response = client.chat.completions.create(
            model="qwen/qwen3-32b",
            messages=messages,
            max_tokens=500,
            temperature=0.3
        )

        final_answer = final_response.choices[0].message.content.strip()

        return ChatResponse(
            answer=final_answer,
            tool_used=tool_name,
            tool_result=tool_result
        )

    # If no tool was used
    return ChatResponse(answer=model_reply)


# ------------------------------------------------
# API ROUTES
# ------------------------------------------------

@app.get("/")
def root():
    return {"status": "Tool Calling API running 🚀"}


@app.get("/health")
def health():
    return {"status": "ok", "model": "qwen/qwen3-32b"}


@app.post("/chat", response_model=ChatResponse)
async def chat_endpoint(request: ChatRequest):

    # Validate message
    if not request.message.strip():
        raise HTTPException(status_code=400, detail="Message cannot be empty")

    # Ensure API key exists
    if not GROQ_API_KEY:
        raise HTTPException(status_code=500, detail="GROQ_API_KEY not configured")

    try:

        result = await chat_with_tools(request.message, request.history)

        return result

    except Exception as e:

        raise HTTPException(status_code=500, detail=str(e))


# ------------------------------------------------
# LOCAL SERVER START
# ------------------------------------------------

if __name__ == "__main__":

    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)