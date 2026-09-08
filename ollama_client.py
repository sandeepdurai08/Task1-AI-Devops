"""
Ollama Integration Module
Handles communication with Ollama for natural language understanding
"""

import requests
import json
from typing import Optional


class OllamaClient:
    """Client for interacting with Ollama API"""
    
    def __init__(self, url: str, model: str):
        """
        Initialize Ollama client
        
        Args:
            url: Ollama server URL (e.g., http://localhost:11434)
            model: Model name to use (e.g., llama3, mistral, codellama)
        """
        self.url = url.rstrip('/')
        self.model = model
        self.system_prompt = self._get_system_prompt()
    
    def _get_system_prompt(self) -> str:
        """Get the system prompt for Jenkins assistant"""
        return """You are a helpful Jenkins CI/CD assistant. Your job is to understand user questions about Jenkins jobs and extract the intent.

When a user asks something, respond with a JSON object containing:
- "intent": one of ["get_job_status", "list_jobs", "get_build_log", "trigger_build", "help", "unknown"]
- "job_name": the job name if mentioned (or null)
- "build_number": the build number if mentioned (or null)
- "response": a brief, friendly response to the user

Examples:
User: "What's the status of my deploy job?"
{"intent": "get_job_status", "job_name": "deploy", "build_number": null, "response": "Let me check the status of the deploy job for you."}

User: "Show me all jobs"
{"intent": "list_jobs", "job_name": null, "build_number": null, "response": "I'll fetch the list of all Jenkins jobs."}

User: "Get logs for build 42 of test-pipeline"
{"intent": "get_build_log", "job_name": "test-pipeline", "build_number": 42, "response": "Fetching the logs for build #42 of test-pipeline."}

User: "Run the build-app job"
{"intent": "trigger_build", "job_name": "build-app", "build_number": null, "response": "I'll trigger a new build for build-app."}

User: "What can you do?"
{"intent": "help", "job_name": null, "build_number": null, "response": "I can help you with Jenkins! I can check job statuses, list all jobs, fetch build logs, and trigger new builds."}

Always respond with valid JSON only, no additional text."""

    def test_connection(self) -> bool:
        """Test if connection to Ollama is working"""
        try:
            response = requests.get(f"{self.url}/api/tags", timeout=10)
            return response.status_code == 200
        except requests.RequestException:
            return False
    
    def list_models(self) -> list[str]:
        """List available models in Ollama"""
        try:
            response = requests.get(f"{self.url}/api/tags", timeout=10)
            response.raise_for_status()
            data = response.json()
            return [model["name"] for model in data.get("models", [])]
        except requests.RequestException as e:
            print(f"Error listing models: {e}")
            return []
    
    def parse_user_intent(self, user_message: str) -> dict:
        """
        Use Ollama to understand user intent
        
        Args:
            user_message: The user's natural language input
            
        Returns:
            Dictionary with intent, job_name, build_number, and response
        """
        try:
            response = requests.post(
                f"{self.url}/api/generate",
                json={
                    "model": self.model,
                    "prompt": user_message,
                    "system": self.system_prompt,
                    "stream": False,
                    "format": "json"
                },
                timeout=60
            )
            response.raise_for_status()
            
            result = response.json()
            response_text = result.get("response", "")
            
            # Parse the JSON response from Ollama
            try:
                parsed = json.loads(response_text)
                return {
                    "intent": parsed.get("intent", "unknown"),
                    "job_name": parsed.get("job_name"),
                    "build_number": parsed.get("build_number"),
                    "response": parsed.get("response", "I understand. Let me help you with that.")
                }
            except json.JSONDecodeError:
                # If Ollama didn't return valid JSON, try to extract intent manually
                return self._fallback_intent_parse(user_message, response_text)
                
        except requests.RequestException as e:
            print(f"Error communicating with Ollama: {e}")
            return {
                "intent": "unknown",
                "job_name": None,
                "build_number": None,
                "response": "I'm having trouble understanding. Could you try rephrasing?"
            }
    
    def _fallback_intent_parse(self, user_message: str, ollama_response: str) -> dict:
        """Fallback intent parsing when Ollama doesn't return valid JSON"""
        message_lower = user_message.lower()
        
        # Simple keyword-based fallback
        if any(word in message_lower for word in ["list", "all jobs", "show jobs"]):
            return {
                "intent": "list_jobs",
                "job_name": None,
                "build_number": None,
                "response": "Let me list all the jobs for you."
            }
        elif any(word in message_lower for word in ["status", "state", "how is"]):
            # Try to extract job name
            job_name = self._extract_job_name(user_message)
            return {
                "intent": "get_job_status",
                "job_name": job_name,
                "build_number": None,
                "response": f"Checking the status{' of ' + job_name if job_name else ''}..."
            }
        elif any(word in message_lower for word in ["log", "output", "console"]):
            job_name = self._extract_job_name(user_message)
            return {
                "intent": "get_build_log",
                "job_name": job_name,
                "build_number": None,
                "response": "Fetching the build logs..."
            }
        elif any(word in message_lower for word in ["run", "trigger", "start", "build"]):
            job_name = self._extract_job_name(user_message)
            return {
                "intent": "trigger_build",
                "job_name": job_name,
                "build_number": None,
                "response": "I'll trigger that build for you."
            }
        elif any(word in message_lower for word in ["help", "what can", "how do"]):
            return {
                "intent": "help",
                "job_name": None,
                "build_number": None,
                "response": ollama_response or "I can help you check job statuses, list jobs, view logs, and trigger builds!"
            }
        else:
            return {
                "intent": "unknown",
                "job_name": None,
                "build_number": None,
                "response": ollama_response or "I'm not sure what you're asking. Try 'help' to see what I can do."
            }
    
    def _extract_job_name(self, message: str) -> Optional[str]:
        """Try to extract a job name from the message"""
        # Common patterns: "of <job>", "for <job>", "<job> job", "<job> status"
        words = message.split()
        
        # Look for quoted names
        import re
        quoted = re.findall(r'["\']([^"\']+)["\']', message)
        if quoted:
            return quoted[0]
        
        # Look for words after "of", "for"
        for i, word in enumerate(words):
            if word.lower() in ["of", "for"] and i + 1 < len(words):
                return words[i + 1].strip("?.,!")
        
        return None
    
    def generate_response(self, prompt: str) -> str:
        """
        Generate a free-form response using Ollama
        
        Args:
            prompt: The prompt to send to Ollama
            
        Returns:
            Generated response text
        """
        try:
            response = requests.post(
                f"{self.url}/api/generate",
                json={
                    "model": self.model,
                    "prompt": prompt,
                    "stream": False
                },
                timeout=60
            )
            response.raise_for_status()
            
            result = response.json()
            return result.get("response", "I couldn't generate a response.")
            
        except requests.RequestException as e:
            return f"Error: {e}"
