"""Local voice bot demo using a LangChain 1.x LCEL chain.

Wraps any LangChain ``Runnable`` (an LCEL chain, a
``RunnableWithMessageHistory``, a LangChain ``AgentExecutor``, etc.) in
``LangChainBridge`` so the voice pipeline can stream text deltas, tool
calls, and cursor transitions into the EasyCat journal.

For stateful multi-node agent workflows see ``langgraph_voice.py``.

Setup: export OPENAI_API_KEY=...; uv sync --extra quickstart --extra langchain --group dev
       uv run easycat doctor
       uv run easycat doctor --env-file .env  # if keys live in .env
       uv run easycat doctor --env-file .env --json  # for parseable checks
Run:   uv run python examples/langchain_voice.py
       uv run --env-file .env python examples/langchain_voice.py  # if keys live in .env
"""

try:
    from langchain_core.prompts import ChatPromptTemplate
    from langchain_openai import ChatOpenAI
except ImportError as exc:
    raise SystemExit(
        "LangChain 1.x is required. For an app, run: "
        "uv add 'easycat[quickstart,langchain]'. In this repo, run: "
        "uv sync --extra quickstart --extra langchain --group dev"
    ) from exc

from easycat import EasyConfig, run

prompt = ChatPromptTemplate.from_messages(
    [
        ("system", "You are a helpful voice assistant. Keep answers short."),
        ("placeholder", "{history}"),
        ("user", "{input}"),
    ]
)
chain = prompt | ChatOpenAI(model="gpt-5.5")

run(EasyConfig.mic(agent=chain))
