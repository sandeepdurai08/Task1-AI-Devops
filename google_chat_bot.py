#!/usr/bin/env python3
"""
Google Chat Bot for Jenkins
Webhook-based integration with Google Chat

Supports two authentication modes:
- WITH AUTH (VERIFY_GOOGLE_TOKEN=true): Production-ready, validates Google's JWT
- WITHOUT AUTH (VERIFY_GOOGLE_TOKEN=false): For testing or internal networks
"""

import os
import json
import logging
from flask import Flask, request, jsonify
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Initialize Flask app
app = Flask(__name__)

# =====================================================
# CONFIGURATION
# =====================================================

# Jenkins Configuration
JENKINS_URL = os.getenv("JENKINS_URL")
JENKINS_USER = os.getenv("JENKINS_USER")
JENKINS_API_TOKEN = os.getenv("JENKINS_API_TOKEN")

# Ollama Configuration  
OLLAMA_URL = os.getenv("OLLAMA_URL", "http://localhost:11434")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "llama3")

# Google Chat Auth Configuration
# ================================
# VERIFY_GOOGLE_TOKEN: 
#   - "true"  = Verify requests are from Google (RECOMMENDED for production)
#   - "false" = Skip verification (for testing/internal use)
#
# GOOGLE_CHAT_PROJECT_NUMBER:
#   - Required when VERIFY_GOOGLE_TOKEN=true
#   - Get from: Google Cloud Console > Project Settings > Project Number
# ================================
GOOGLE_CHAT_PROJECT_NUMBER = os.getenv("GOOGLE_CHAT_PROJECT_NUMBER")
VERIFY_GOOGLE_TOKEN = os.getenv("VERIFY_GOOGLE_TOKEN", "false").lower() == "true"

# Global clients (initialized on startup)
jenkins_client = None
ollama_client = None
card_builder = None


def init_clients():
    """Initialize Jenkins and Ollama clients"""
    global jenkins_client, ollama_client, card_builder
    
    from jenkins_client import JenkinsClient
    from ollama_client import OllamaClient
    from chat_cards import ChatCardBuilder
    
    card_builder = ChatCardBuilder()
    
    if all([JENKINS_URL, JENKINS_USER, JENKINS_API_TOKEN]):
        jenkins_client = JenkinsClient(JENKINS_URL, JENKINS_USER, JENKINS_API_TOKEN)
        logger.info(f"✅ Jenkins client initialized for {JENKINS_URL}")
    else:
        logger.error("❌ Missing Jenkins configuration (JENKINS_URL, JENKINS_USER, JENKINS_API_TOKEN)")
    
    ollama_client = OllamaClient(OLLAMA_URL, OLLAMA_MODEL)
    logger.info(f"✅ Ollama client initialized ({OLLAMA_MODEL} at {OLLAMA_URL})")


def verify_google_chat_request(req):
    """
    Verify that the request comes from Google Chat
    
    ┌─────────────────────────────────────────────────────────────┐
    │ MODE 1: NO AUTH (VERIFY_GOOGLE_TOKEN=false)                 │
    │ - Skips verification                                        │
    │ - Good for: testing, internal networks, quick prototyping   │
    │                                                             │
    │ MODE 2: WITH AUTH (VERIFY_GOOGLE_TOKEN=true)                │
    │ - Validates JWT token from Google                           │
    │ - Requires: GOOGLE_CHAT_PROJECT_NUMBER                      │
    │ - Good for: production, public-facing bots                  │
    └─────────────────────────────────────────────────────────────┘
    
    Returns:
        bool: True if verified (or verification disabled), False otherwise
    """
    
    # ============================================
    # MODE 1: NO AUTH (for testing or internal use)
    # ============================================
    if not VERIFY_GOOGLE_TOKEN:
        logger.info("🔓 Auth DISABLED (VERIFY_GOOGLE_TOKEN=false) - request allowed")
        return True
    
    # ============================================
    # MODE 2: WITH AUTH (recommended for production)
    # ============================================
    logger.info("🔒 Auth ENABLED - verifying Google token...")
    
    # Check if project number is configured
    if not GOOGLE_CHAT_PROJECT_NUMBER:
        logger.error("❌ VERIFY_GOOGLE_TOKEN=true but GOOGLE_CHAT_PROJECT_NUMBER not set!")
        logger.error("   Get it from: Google Cloud Console > Project Settings")
        return False
    
    # Import auth libraries only when needed
    try:
        from google.oauth2 import id_token
        from google.auth.transport import requests as google_requests
    except ImportError:
        logger.error("❌ google-auth library not installed. Run: pip install google-auth")
        return False
    
    # Get the bearer token from Authorization header
    auth_header = req.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        logger.error("❌ Missing or invalid Authorization header")
        return False
    
    token = auth_header[7:]  # Remove "Bearer " prefix
    
    try:
        # Verify the token
        decoded_token = id_token.verify_token(
            token,
            google_requests.Request(),
            audience=GOOGLE_CHAT_PROJECT_NUMBER,
            certs_url="https://www.googleapis.com/service_accounts/v1/metadata/x509/chat@system.gserviceaccount.com"
        )
        
        # Check the issuer
        if decoded_token.get("iss") != "chat@system.gserviceaccount.com":
            logger.error(f"❌ Invalid issuer: {decoded_token.get('iss')}")
            return False
        
        logger.info("✅ Google Chat token verified successfully")
        return True
        
    except Exception as e:
        logger.error(f"❌ Token verification failed: {e}")
        return False


@app.route("/", methods=["POST"])
def handle_chat():
    """
    Main webhook endpoint for Google Chat
    
    Google Chat sends events here when:
    - User sends a message to the bot (MESSAGE)
    - User adds bot to a space (ADDED_TO_SPACE)
    - User removes bot from a space (REMOVED_FROM_SPACE)
    - User clicks a button on a card (CARD_CLICKED)
    """
    # Verify the request is from Google (if auth enabled)
    if not verify_google_chat_request(request):
        return jsonify({"text": "🚫 Unauthorized request"}), 401
    
    try:
        event = request.get_json()
        event_type = event.get("type", "")
        
        logger.info(f"📨 Received event: {event_type}")
        
        # Route to appropriate handler
        if event_type == "ADDED_TO_SPACE":
            return handle_added_to_space(event)
        
        elif event_type == "REMOVED_FROM_SPACE":
            return handle_removed_from_space(event)
        
        elif event_type == "MESSAGE":
            return handle_message(event)
        
        elif event_type == "CARD_CLICKED":
            return handle_card_click(event)
        
        else:
            logger.warning(f"⚠️ Unknown event type: {event_type}")
            return jsonify({"text": "Unknown event type"})
    
    except Exception as e:
        logger.error(f"💥 Error processing request: {e}", exc_info=True)
        return jsonify({"text": f"Error: {str(e)}"}), 500


def handle_added_to_space(event):
    """Handle bot being added to a space"""
    space = event.get("space", {})
    user = event.get("user", {})
    
    logger.info(f"👋 Bot added to '{space.get('displayName')}' by {user.get('displayName')}")
    
    return jsonify({
        "text": "👋 Hello! I'm your Jenkins Assistant.\n\n"
                "I can help you:\n"
                "• Check job status - \"status of my-job\"\n"
                "• List all jobs - \"list jobs\"\n"
                "• View build logs - \"logs for my-job\"\n"
                "• Trigger builds - \"run my-job\"\n\n"
                "Just ask me in natural language!"
    })


def handle_removed_from_space(event):
    """Handle bot being removed from a space"""
    space = event.get("space", {})
    logger.info(f"👋 Bot removed from '{space.get('displayName')}'")
    return jsonify({})


def handle_message(event):
    """Handle incoming message from user"""
    message = event.get("message", {})
    user_text = message.get("argumentText", "").strip() or message.get("text", "").strip()
    user = event.get("user", {})
    
    logger.info(f"💬 Message from {user.get('displayName')}: {user_text}")
    
    if not user_text:
        return jsonify({"text": "I didn't catch that. Try asking about Jenkins jobs!"})
    
    # Check if Jenkins is configured
    if not jenkins_client:
        return jsonify({
            "text": "⚠️ Jenkins is not configured. Please check environment variables."
        })
    
    # Use Ollama to understand the intent
    intent_data = ollama_client.parse_user_intent(user_text)
    intent = intent_data.get("intent", "unknown")
    job_name = intent_data.get("job_name")
    build_number = intent_data.get("build_number")
    ai_response = intent_data.get("response", "")
    
    logger.info(f"🤖 Parsed intent: {intent}, job: {job_name}")
    
    # Execute based on intent
    if intent == "list_jobs":
        jobs = jenkins_client.get_all_jobs()
        return jsonify(card_builder.build_job_list_card(jobs))
    
    elif intent == "get_job_status":
        if not job_name:
            jobs = jenkins_client.get_all_jobs()
            return jsonify({
                "text": "Which job would you like to check? Here are the available jobs:",
                "cardsV2": card_builder.build_job_list_card(jobs).get("cardsV2", [])
            })
        
        status = jenkins_client.get_job_status(job_name)
        if status:
            return jsonify(card_builder.build_job_status_card(status))
        else:
            return jsonify({"text": f"❌ Job '{job_name}' not found. Try 'list jobs' to see available jobs."})
    
    elif intent == "get_build_log":
        if not job_name:
            return jsonify({"text": "Please specify a job name. Example: 'logs for my-job'"})
        
        log = jenkins_client.get_build_log(job_name, build_number)
        if log:
            # Truncate log for chat display
            max_chars = 2000
            if len(log) > max_chars:
                log = f"...(truncated)\n\n{log[-max_chars:]}"
            
            return jsonify({
                "text": f"📜 **Build Log: {job_name}** #{build_number or 'latest'}\n```\n{log}\n```"
            })
        else:
            return jsonify({"text": f"❌ Could not fetch logs for '{job_name}'"})
    
    elif intent == "trigger_build":
        if not job_name:
            return jsonify({"text": "Please specify which job to build. Example: 'run my-job'"})
        
        # Return a confirmation card with button
        return jsonify(card_builder.build_trigger_confirmation_card(job_name))
    
    elif intent == "help":
        return jsonify(card_builder.build_help_card())
    
    else:
        return jsonify({
            "text": f"🤔 {ai_response}\n\nTry asking:\n• \"list jobs\"\n• \"status of <job-name>\"\n• \"run <job-name>\""
        })


def handle_card_click(event):
    """Handle button clicks on cards"""
    action = event.get("action", {})
    action_name = action.get("actionMethodName", "")
    parameters = {p["key"]: p["value"] for p in action.get("parameters", [])}
    
    logger.info(f"🖱️ Card action: {action_name}, params: {parameters}")
    
    if action_name == "trigger_build":
        job_name = parameters.get("job_name")
        if job_name and jenkins_client:
            success = jenkins_client.trigger_build(job_name)
            if success:
                return jsonify({"text": f"✅ Build triggered for **{job_name}**!"})
            else:
                return jsonify({"text": f"❌ Failed to trigger build for {job_name}"})
    
    elif action_name == "cancel_build":
        return jsonify({"text": "Build cancelled."})
    
    elif action_name == "refresh_status":
        job_name = parameters.get("job_name")
        if job_name and jenkins_client:
            status = jenkins_client.get_job_status(job_name)
            if status:
                return jsonify(card_builder.build_job_status_card(status))
    
    return jsonify({"text": "Action processed."})


@app.route("/health", methods=["GET"])
def health_check():
    """Health check endpoint"""
    jenkins_ok = jenkins_client.test_connection() if jenkins_client else False
    ollama_ok = ollama_client.test_connection() if ollama_client else False
    
    status = "healthy" if (jenkins_ok and ollama_ok) else "degraded"
    
    return jsonify({
        "status": status,
        "jenkins": "connected" if jenkins_ok else "disconnected",
        "ollama": "connected" if ollama_ok else "disconnected",
        "auth_enabled": VERIFY_GOOGLE_TOKEN
    })


# Initialize clients when module loads
init_clients()


if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))
    debug = os.getenv("FLASK_DEBUG", "false").lower() == "true"
    
    print("\n" + "="*60)
    print("🤖 JENKINS GOOGLE CHAT BOT")
    print("="*60)
    print(f"📍 Running on: http://0.0.0.0:{port}")
    print(f"🔐 Auth Mode: {'ENABLED' if VERIFY_GOOGLE_TOKEN else 'DISABLED'}")
    print(f"🏗️  Jenkins: {JENKINS_URL}")
    print(f"🧠 Ollama: {OLLAMA_URL} ({OLLAMA_MODEL})")
    print("="*60 + "\n")
    
    app.run(host="0.0.0.0", port=port, debug=debug)
