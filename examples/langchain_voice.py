"""Local voice bot demo using a LangChain LCEL chain.

Wraps any LangChain ``Runnable`` (an LCEL chain, a
``RunnableWithMessageHistory``, a LangChain ``AgentExecutor``, etc.) in
``LangChainBridge`` so the voice pipeline can stream text deltas, tool
calls, and cursor transitions into the EasyCat journal.

For stateful multi-node agent workflows see
``examples/langgraph_voice.py`` instead.

Setup:
  export OPENAI_API_KEY="..."
  uv sync --extra quickstart
  uv add easycat[langchain] langchain-openai
  uv run python examples/langchain_voice.py
"""

from __future__ import annotations

import asyncio

from easycat import (
    EasyCatConfig,
    LocalTransportConfig,
    attach_runtime_feedback,
    create_session,
    require_env,
    wait_for_shutdown_signal,
)


async def main() -> None:
    api_key = require_env("OPENAI_API_KEY")

    try:
        from langchain_core.prompts import ChatPromptTemplate
        from langchain_openai import ChatOpenAI
    except ImportError as exc:
        raise SystemExit(
            "LangChain is required. Install with: uv add easycat[langchain] langchain-openai"
        ) from exc

    prompt = ChatPromptTemplate.from_messages(
        [
            ("system", "You are a helpful voice assistant. Keep answers short."),
            ("placeholder", "{history}"),
            ("user", "{input}"),
        ]
    )
    model = ChatOpenAI(model="gpt-4o-mini", api_key=api_key)
    chain = prompt | model

    config = EasyCatConfig(
        openai_api_key=api_key,
        transport=LocalTransportConfig(),
        agent=chain,  # auto_adapt_agent() routes through LangChainBridge
    )
    session = create_session(config)
    attach_runtime_feedback(session)

    await session.start()
    await wait_for_shutdown_signal(session)


if __name__ == "__main__":
    asyncio.run(main())
