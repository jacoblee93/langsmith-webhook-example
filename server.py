import httpx
import json
import os

from dotenv import load_dotenv
from fastapi import FastAPI, BackgroundTasks
import openai
from typing import Any

load_dotenv()

app = FastAPI()

evaluator_prompt = None


def fill_template(template: str, variables: dict[str, Any]) -> str:
    """Fill mustache-style template with variables"""
    result = template
    for key, value in variables.items():
        # Handle mustache syntax {{key}}
        result = result.replace(f"{{{{{key}}}}}", str(value))
    return result


def convert_langsmith_to_openai(
    langsmith_data: dict[str, Any], run_data: dict[str, Any] = None
) -> dict[str, Any]:
    """Convert LangSmith prompt format to OpenAI chat completion format"""
    commit = langsmith_data["commits"][0]
    manifest = commit["manifest"]["kwargs"]

    # Extract the structured prompt
    structured_prompt = manifest["first"]["kwargs"]
    messages = structured_prompt["messages"]
    schema = structured_prompt["schema_"]

    # Build OpenAI messages array
    openai_messages = []
    for msg in messages:
        role = "system" if "SystemMessage" in msg["id"][2] else "user"
        template = msg["kwargs"]["prompt"]["kwargs"]["template"]

        # Fill template with run data if provided
        if run_data:
            # Template in whatever you want
            template_vars = {
                "inputs": json.dumps(run_data.get("inputs", {})),
                "outputs": json.dumps(run_data.get("outputs", {})),
            }
            template = fill_template(template, template_vars)

        openai_messages.append({"role": role, "content": template})

    # Extract model info
    model_kwargs = manifest["last"]["kwargs"]
    model = model_kwargs.get("model", "gpt-4")

    # Ensure schema has additionalProperties set to false
    if "additionalProperties" not in schema:
        schema["additionalProperties"] = False

    return {
        "model": model,
        "messages": openai_messages,
        "response_format": {
            "type": "json_schema",
            "json_schema": {
                "name": schema["title"],
                "schema": schema,
                "strict": schema.get("strict", True),
            },
        },
    }


async def fetch_evaluator_prompt():
    # Update with whatever prompt you want to use
    # For private prompts, use `-` for the owner
    owner = "jacob"
    prompt_name = "simple-public-evaluator"

    api_key = os.getenv("LANGSMITH_API_KEY")
    headers = {"X-Api-Key": api_key}

    async with httpx.AsyncClient() as client:
        response = await client.get(
            f"https://api.smith.langchain.com/commits/{owner}/{prompt_name}?include_model=true",
            headers=headers,
        )
        langsmith_data = response.json()
        return langsmith_data


async def create_langsmith_feedback(
    run_id: str, trace_id: str, key: str, score: bool | int, comment: str = None
):
    api_key = os.getenv("LANGSMITH_API_KEY")
    headers = {"X-Api-Key": api_key, "Content-Type": "application/json"}

    feedback_data = {
        "key": key,
        "score": score,
        "run_id": run_id,
        "trace_id": trace_id,
        "feedback_source": {
            "type": "api",
        },
    }
    if comment:
        feedback_data["comment"] = comment

    async with httpx.AsyncClient() as client:
        response = await client.post(
            "https://api.smith.langchain.com/api/v1/feedback",
            headers=headers,
            json=feedback_data,
        )
        return response.json()


async def process_webhook(payload: dict[str, Any]):
    global evaluator_prompt

    rule_id = payload.get("rule_id")
    runs = payload.get("runs", [])

    if not evaluator_prompt:
        evaluator_prompt = await fetch_evaluator_prompt()

    print(f"Processing webhook for rule {rule_id}")
    print(f"Number of runs: {len(runs)}")

    for run in runs:
        run_id = run.get("id")
        trace_id = run.get("trace_id")
        print(f"Processing run: {run_id}")

        # Convert prompt with run data
        openai_params = convert_langsmith_to_openai(evaluator_prompt, run)

        print("OpenAI params: ", openai_params)

        # Evaluate with OpenAI
        response = openai.chat.completions.create(**openai_params)
        result_json = response.choices[0].message.content
        eval_result = json.loads(result_json)
        print(f"Evaluation result: {eval_result}")

        # Create feedback for each key in the result
        for key, score in eval_result.items():
            feedback = await create_langsmith_feedback(
                run_id,
                trace_id,
                key,
                score,
                comment="Evaluated by custom webhook evaluator",
            )
            print(f"Created feedback: {feedback}")


@app.post("/webhook")
async def webhook(payload: dict[str, Any], background_tasks: BackgroundTasks):
    background_tasks.add_task(process_webhook, payload)
    return {"message": "Webhook received"}
