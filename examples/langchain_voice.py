"""Local voice bot demo using a LangChain LCEL chain.

Wraps any LangChain ``Runnable`` (an LCEL chain, a
``RunnableWithMessageHistory``, a LangChain ``AgentExecutor``, etc.) in
``LangChainBridge`` so the voice pipeline can stream text deltas, tool
calls, and cursor transitions into the EasyCat journal.

For stateful multi-node agent workflows see ``langgraph_voice.py``.

Setup: export OPENAI_API_KEY=...; uv sync --extra quickstart --extra langchain --group dev
       uv pip install langchain-openai
       uv run easycat doctor
       uv run easycat doctor --env-file .env  # if keys live in .env
Run:   uv run python examples/langchain_voice.py
       uv run --env-file .env python examples/langchain_voice.py  # if keys live in .env
"""

try:
    from langchain_core.prompts import ChatPromptTemplate
    from langchain_openai import ChatOpenAI
except ImportError as exc:
    raise SystemExit(
        "LangChain is required. For an app, run: "
        "uv add 'easycat[quickstart,langchain]' langchain-openai. In this repo, run: "
        "uv sync --extra quickstart --extra langchain --group dev; uv pip install langchain-openai"
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
