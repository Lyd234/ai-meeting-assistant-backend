import asyncio
import os
import logging
from uuid import uuid4
from dotenv import load_dotenv
from fastapi import FastAPI, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware

from vision_agents.core import agents
from vision_agents.plugins import getstream, gemini
from vision_agents.core.edge.types import User
from vision_agents.core.events import (
    CallSessionStartedEvent,
    CallSessionEndedEvent,
)
from vision_agents.core.llm.events import RealtimeUserSpeechTranscriptionEvent

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)
load_dotenv()

app = FastAPI(title="Meeting Assistant Backend")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

active_agents = {}

async def start_agent(call_id: str):
    logger.info(f"🤖 Starting agent for: {call_id}")
    
    meeting_data = {"transcript": [], "is_active": False}
    
    agent = agents.Agent(
        edge=getstream.Edge(),
        agent_user=User(id="meeting-assistant-bot", name="Meeting Assistant"),
        instructions="""You are a meeting transcription bot.
        ONLY speak when someone says 'Hey Assistant'.
        Otherwise, stay silent and transcribe.""",
        llm=gemini.Realtime(fps=0),
    )
    
    @agent.events.subscribe
    async def handle_session_started(event: CallSessionStartedEvent):
        meeting_data["is_active"] = True
        logger.info("🎙️ Meeting started")
    
    @agent.events.subscribe
    async def handle_transcript(event: RealtimeUserSpeechTranscriptionEvent):
        if not event.text or not event.text.strip():
            return
        
        speaker = getattr(event, 'participant_id', 'Unknown')
        meeting_data["transcript"].append({"speaker": speaker, "text": event.text})
        logger.info(f"📝 [{speaker}]: {event.text}")
        
        if event.text.lower().startswith("hey assistant"):
            question = event.text[13:].strip()
            if question:
                context = "\n".join([f"[{e['speaker']}]: {e['text']}" for e in meeting_data["transcript"]])
                await agent.simple_response(f"{context}\n\nQUESTION: {question}")
    
    @agent.events.subscribe
    async def handle_session_ended(event: CallSessionEndedEvent):
        meeting_data["is_active"] = False
        logger.info("🛑 Meeting ended")
        if call_id in active_agents:
            del active_agents[call_id]
    
    await agent.create_user()
    call = agent.edge.client.video.call("default", call_id)
    active_agents[call_id] = {"agent": agent, "data": meeting_data}
    
    async with await agent.join(call):
        await agent.finish()

@app.get("/")
async def root():
    return {"status": "online", "service": "Meeting Assistant", "active": len(active_agents)}

@app.get("/health")
async def health():
    return {"status": "healthy", "active_meetings": list(active_agents.keys())}

@app.post("/start-agent/{call_id}")
async def start_agent_endpoint(call_id: str, background_tasks: BackgroundTasks):
    if call_id in active_agents:
        return {"status": "already_active", "call_id": call_id}
    background_tasks.add_task(start_agent, call_id)
    return {"status": "starting", "call_id": call_id}

if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
