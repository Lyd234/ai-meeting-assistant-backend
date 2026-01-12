import asyncio
import os
import logging
from dotenv import load_dotenv
from fastapi import FastAPI
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

class DummyLLM:
    """A placeholder LLM when API quota is exceeded"""
    
    # Define as class properties
    audio_in = True
    audio_out = False
    video_in = False
    video_out = False
    
    def __init__(self):
        self.quota_exceeded = True
        logger.warning("⚠️ Using DummyLLM - API quota exceeded or unavailable")
    
    def _attach_agent(self, agent):
        logger.info("✅ DummyLLM attached to agent")
    
    async def connect(self):
        """Mock connect method - does nothing"""
        logger.info("✅ DummyLLM connected (no-op)")
        pass
    
    async def disconnect(self):
        """Mock disconnect method - does nothing"""
        pass
    
    async def generate(self, *args, **kwargs):
        return "⚠️ API quota exceeded. Transcription continues but AI responses are unavailable."

async def start_agent(call_id: str):
    logger.info(f"🤖 Starting agent for call: {call_id}")
    
    meeting_data = {"transcript": [], "is_active": False, "quota_exceeded": False}
    
    # Try with Gemini first
    try:
        logger.info("🔄 Attempting to use Gemini LLM...")
        llm = gemini.Realtime(fps=0)
        
        agent = agents.Agent(
            edge=getstream.Edge(),
            agent_user=User(id="meeting-assistant-bot", name="Meeting Assistant"),
            instructions="""You are a meeting transcription bot.
            ONLY speak when someone says 'Hey Assistant'.
            Otherwise, stay silent and transcribe.""",
            llm=llm,
        )
        
        # Set up event handlers
        setup_event_handlers(agent, meeting_data, call_id)
        
        # Create user and join call
        logger.info("📝 Creating agent user...")
        await agent.create_user()
        
        logger.info("📞 Getting call reference...")
        call = agent.edge.client.video.call("default", call_id)
        
        logger.info("🚀 Attempting to join call with Gemini...")
        
        try:
            async with await agent.join(call):
                logger.info(f"✅ AGENT SUCCESSFULLY JOINED CALL: {call_id} (Full AI mode)")
                logger.info("🎙️ Agent is now active and listening...")
                
                # Keep agent alive
                await agent.finish()
            
            logger.info("👋 Agent left the call")
            
        except Exception as join_error:
            error_msg = str(join_error).lower()
            
            # Check if it's a quota error
            if "quota" in error_msg or "rate limit" in error_msg or "1011" in error_msg:
                logger.warning(f"⚠️ Gemini API quota exceeded: {join_error}")
                logger.warning("="*60)
                logger.warning("API QUOTA EXCEEDED")
                logger.warning("The Meeting Assistant cannot join the call.")
                logger.warning("Users will not see the bot in the meeting.")
                logger.warning("To fix: Check your Gemini API quota and billing.")
                logger.warning("="*60)
                
                # Clean up the failed agent
                try:
                    await agent.edge.client.disconnect()
                except:
                    pass
                
                logger.info("❌ Bot cannot join without API quota - exiting")
            else:
                raise  # Re-raise if it's not a quota error
        
    except asyncio.CancelledError:
        logger.info(f"⚠️ Agent task cancelled for call: {call_id}")
        raise
    except Exception as e:
        logger.error(f"❌ Error in start_agent: {e}")
        import traceback
        logger.error(traceback.format_exc())
    finally:
        if call_id in active_agents:
            del active_agents[call_id]
            logger.info(f"🧹 Cleaned up agent for call: {call_id}")

def setup_event_handlers(agent, meeting_data, call_id):
    """Set up event handlers for the agent"""
    
    @agent.events.subscribe
    async def handle_session_started(event: CallSessionStartedEvent):
        meeting_data["is_active"] = True
        
        quota_exceeded = meeting_data.get("quota_exceeded", False)
        
        if quota_exceeded:
            logger.info("🎙️ Meeting session started (Transcription-only mode - API quota exceeded)")
        else:
            logger.info("🎙️ Meeting session started (Full AI mode)")
        
        # Initialize chat channel
        try:
            channel = agent.edge.client.channel("messaging", call_id)
            await channel.watch()
            meeting_data["channel"] = channel
            logger.info("✅ Chat channel initialized")
            
            # Send status message to users - THIS IS WHERE THE USER SEES THE MESSAGE
            if quota_exceeded:
                await channel.send_message({
                    "text": "⚠️ **API Quota Exceeded** - Meeting Assistant joined in transcription-only mode. Your conversation will be transcribed, but I cannot answer questions at this time.",
                })
                logger.info("📤 Sent quota exceeded notification to users")
            else:
                await channel.send_message({
                    "text": "✅ Meeting Assistant joined! Say 'Hey Assistant' followed by your question to interact with me.",
                })
                logger.info("📤 Sent welcome message to users")
        except Exception as e:
            logger.error(f"❌ Chat channel error: {e}")
    
    @agent.events.subscribe
    async def handle_transcript(event: RealtimeUserSpeechTranscriptionEvent):
        if not event.text or not event.text.strip():
            return
        
        speaker = getattr(event, 'participant_id', 'Unknown')
        meeting_data["transcript"].append({"speaker": speaker, "text": event.text})
        logger.info(f"📝 [{speaker}]: {event.text}")
        
        quota_exceeded = meeting_data.get("quota_exceeded", False)
        
        if event.text.lower().startswith("hey assistant"):
            question = event.text[13:].strip()
            if question:
                logger.info(f"❓ Question detected: {question}")
                
                if quota_exceeded:
                    # Send quota exceeded message when user tries to ask a question
                    try:
                        if "channel" in meeting_data:
                            await meeting_data["channel"].send_message({
                                "text": "⚠️ Sorry, I cannot answer questions right now due to API quota limits. However, I'm still transcribing the meeting for you.",
                            })
                            logger.info("📤 Sent quota exceeded response to question")
                    except Exception as e:
                        logger.error(f"❌ Failed to send chat message: {e}")
                else:
                    # Process question with AI
                    context = "\n".join([f"[{e['speaker']}]: {e['text']}" for e in meeting_data["transcript"]])
                    
                    try:
                        if "channel" in meeting_data:
                            await meeting_data["channel"].send_message({
                                "text": f"🤔 Processing question: {question}",
                            })
                    except Exception as e:
                        logger.error(f"❌ Failed to send chat message: {e}")
                    
                    try:
                        await agent.simple_response(f"{context}\n\nQUESTION: {question}")
                    except Exception as e:
                        logger.error(f"❌ Error processing AI response: {e}")
                        if "channel" in meeting_data:
                            await meeting_data["channel"].send_message({
                                "text": f"❌ Error processing your question: {str(e)}",
                            })
    
    @agent.events.subscribe
    async def handle_session_ended(event: CallSessionEndedEvent):
        meeting_data["is_active"] = False
        logger.info("🛑 Meeting session ended")
        if call_id in active_agents:
            del active_agents[call_id]

@app.get("/")
async def root():
    return {
        "status": "online", 
        "service": "Meeting Assistant", 
        "active_meetings": len(active_agents),
        "call_ids": list(active_agents.keys())
    }

@app.get("/health")
async def health():
    return {
        "status": "healthy", 
        "active_meetings": list(active_agents.keys()),
        "count": len(active_agents)
    }

@app.post("/start-agent/{call_id}")
async def start_agent_endpoint(call_id: str):
    logger.info(f"📥 Received request to start agent for call: {call_id}")
    
    if call_id in active_agents:
        logger.warning(f"⚠️ Agent already active for call: {call_id}")
        return {"status": "already_active", "call_id": call_id}
    
    # Create placeholder first
    active_agents[call_id] = {"status": "starting"}
    
    # Create the task
    task = asyncio.create_task(start_agent(call_id))
    
    # Update with task reference
    active_agents[call_id]["task"] = task
    
    logger.info(f"✅ Agent task created for call: {call_id}")
    
    return {"status": "starting", "call_id": call_id}

@app.delete("/stop-agent/{call_id}")
async def stop_agent_endpoint(call_id: str):
    logger.info(f"🛑 Request to stop agent for call: {call_id}")
    
    if call_id in active_agents:
        logger.info(f"🔍 Found active agent for call: {call_id}")
        
        # Cancel the task if it exists
        if "task" in active_agents[call_id]:
            task = active_agents[call_id]["task"]
            if not task.done():
                logger.info(f"❌ Cancelling agent task for call: {call_id}")
                task.cancel()
            else:
                logger.info(f"✓ Task already completed for call: {call_id}")
        
        # Remove from active agents
        del active_agents[call_id]
        logger.info(f"✅ Agent stopped and removed for call: {call_id}")
        
        return {"status": "stopped", "call_id": call_id}
    
    logger.warning(f"⚠️ No active agent found for call: {call_id}")
    return {"status": "not_found", "call_id": call_id}

@app.post("/cleanup")
async def cleanup_all():
    logger.info("🧹 Request to clean up all agents")
    count = len(active_agents)
    
    if count == 0:
        logger.info("✓ No active agents to clean up")
        return {"status": "cleaned", "removed": 0}
    
    logger.info(f"🔍 Found {count} active agent(s) to clean up")
    
    for call_id in list(active_agents.keys()):
        logger.info(f"  - Cleaning up agent for call: {call_id}")
        if "task" in active_agents[call_id]:
            task = active_agents[call_id]["task"]
            if not task.done():
                task.cancel()
                logger.info(f"    ✓ Task cancelled for: {call_id}")
            else:
                logger.info(f"    ✓ Task already done for: {call_id}")
    
    active_agents.clear()
    logger.info(f"✅ Cleaned up {count} agent(s)")
    
    return {"status": "cleaned", "removed": count}

@app.get("/agents")
async def list_agents():
    """List all active agents with their status"""
    agents_info = {}
    
    for call_id, agent_data in active_agents.items():
        task_status = "unknown"
        if "task" in agent_data:
            task = agent_data["task"]
            if task.done():
                task_status = "completed"
            elif task.cancelled():
                task_status = "cancelled"
            else:
                task_status = "running"
        
        agents_info[call_id] = {
            "status": agent_data.get("status", "unknown"),
            "task_status": task_status
        }
    
    return {
        "count": len(active_agents),
        "agents": agents_info
    }

if __name__ == "__main__":
    import uvicorn
    logger.info("🚀 Starting Meeting Assistant Backend...")
    logger.info("=" * 60)
    logger.info("Available endpoints:")
    logger.info("  GET  /           - Service status")
    logger.info("  GET  /health     - Health check")
    logger.info("  GET  /agents     - List active agents")
    logger.info("  POST /start-agent/{call_id} - Start agent for a call")
    logger.info("  DELETE /stop-agent/{call_id} - Stop agent for a call")
    logger.info("  POST /cleanup    - Stop all agents")
    logger.info("=" * 60)
    
    port = int(os.getenv("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="info")

