"""
LLM Client Module
Handles communication with OpenAI-compatible API endpoints
Works with: OpenAI, Azure OpenAI, vLLM, LocalAI, LM Studio, custom endpoints
"""

import requests
import json
from typing import Optional


class LLMClient:
    """Client for interacting with OpenAI-compatible APIs"""
    
    def __init__(self, url: str, model: str, api_key: str = None):
        """
        Initialize LLM client
        
        Args:
            url: API endpoint URL (e.g., https://xxx.xxx.info/v1/chat/completions)
            model: Model path/name (e.g., /models/Asn-dsk-Instruct-2507)
            api_key: Optional API key for authentication
        """
        self.url = url.rstrip('/')
        self.model = model
        self.api_key = api_key
        self.system_prompt = self._get_system_prompt()
    
    def _get_headers(self) -> dict:
        """Get request headers"""
        headers = {
            "Content-Type": "application/json"
        }
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers
    
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

Always respond with valid JSON only, no additional text or markdown."""

    def test_connection(self) -> bool:
        """Test if connection to LLM API is working"""
        try:
            # Try a simple completion request
            response = requests.post(
                self.url,
                headers=self._get_headers(),
                json={
                    "model": self.model,
                    "messages": [{"role": "user", "content": "hi"}],
                    "max_tokens": 10
                },
                timeout=30
            )
            return response.status_code == 200
        except requests.RequestException:
            return False
    
    def parse_user_intent(self, user_message: str) -> dict:
        """
        Use LLM to understand user intent
        
        Args:
            user_message: The user's natural language input
            
        Returns:
            Dictionary with intent, job_name, build_number, and response
        """
        try:
            response = requests.post(
                self.url,
                headers=self._get_headers(),
                json={
                    "model": self.model,
                    "messages": [
                        {"role": "system", "content": self.system_prompt},
                        {"role": "user", "content": user_message}
                    ],
                    "temperature": 0.1,  # Low temperature for consistent JSON
                    "max_tokens": 200
                },
                timeout=60
            )
            response.raise_for_status()
            
            result = response.json()
            
            # Extract the assistant's response
            # OpenAI format: result["choices"][0]["message"]["content"]
            response_text = result.get("choices", [{}])[0].get("message", {}).get("content", "")
            
            # Clean up response (remove markdown code blocks if present)
            response_text = response_text.strip()
            if response_text.startswith("```"):
                # Remove ```json and ``` markers
                lines = response_text.split("\n")
                response_text = "\n".join(
                    line for line in lines 
                    if not line.strip().startswith("```")
                )
            
            # Parse the JSON response
            try:
                parsed = json.loads(response_text)
                return {
                    "intent": parsed.get("intent", "unknown"),
                    "job_name": parsed.get("job_name"),
                    "build_number": parsed.get("build_number"),
                    "response": parsed.get("response", "I understand. Let me help you with that.")
                }
            except json.JSONDecodeError:
                # If LLM didn't return valid JSON, try fallback parsing
                return self._fallback_intent_parse(user_message, response_text)
                
        except requests.RequestException as e:
            print(f"Error communicating with LLM: {e}")
            return self._fallback_intent_parse(user_message, "")
    
    def _fallback_intent_parse(self, user_message: str, llm_response: str) -> dict:
        """Fallback intent parsing when LLM doesn't return valid JSON"""
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
                "response": llm_response or "I can help you check job statuses, list jobs, view logs, and trigger builds!"
            }
        else:
            return {
                "intent": "unknown",
                "job_name": None,
                "build_number": None,
                "response": llm_response or "I'm not sure what you're asking. Try 'help' to see what I can do."
            }
    
    def _extract_job_name(self, message: str) -> Optional[str]:
        """Try to extract a job name from the message"""
        import re
        
        # Look for quoted names
        quoted = re.findall(r'["\']([^"\']+)["\']', message)
        if quoted:
            return quoted[0]
        
        # Look for words after "of", "for"
        words = message.split()
        for i, word in enumerate(words):
            if word.lower() in ["of", "for"] and i + 1 < len(words):
                return words[i + 1].strip("?.,!")
        
        return None
    
    def generate_response(self, prompt: str) -> str:
        """
        Generate a free-form response using the LLM
        
        Args:
            prompt: The prompt to send
            
        Returns:
            Generated response text
        """
        try:
            response = requests.post(
                self.url,
                headers=self._get_headers(),
                json={
                    "model": self.model,
                    "messages": [{"role": "user", "content": prompt}],
                    "max_tokens": 500
                },
                timeout=60
            )
            response.raise_for_status()
            
            result = response.json()
            return result.get("choices", [{}])[0].get("message", {}).get("content", "I couldn't generate a response.")
            
        except requests.RequestException as e:
            return f"Error: {e}"
