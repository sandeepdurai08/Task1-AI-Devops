#!/usr/bin/env python3
"""
Jenkins Chatbot - Interactive AI-powered Jenkins assistant
Uses Ollama for natural language understanding and Jenkins API for job management
"""

import os
import sys
from dotenv import load_dotenv
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.markdown import Markdown
from rich.prompt import Prompt

from jenkins_client import JenkinsClient, JobStatus
from ollama_client import OllamaClient


class JenkinsChatbot:
    """Interactive chatbot for Jenkins operations"""
    
    def __init__(self):
        self.console = Console()
        self.jenkins: JenkinsClient = None
        self.ollama: OllamaClient = None
        
    def setup(self) -> bool:
        """Initialize connections to Jenkins and Ollama"""
        load_dotenv()
        
        # Get configuration from environment
        jenkins_url = os.getenv("JENKINS_URL")
        jenkins_user = os.getenv("JENKINS_USER")
        jenkins_token = os.getenv("JENKINS_API_TOKEN")
        ollama_url = os.getenv("OLLAMA_URL", "http://localhost:11434")
        ollama_model = os.getenv("OLLAMA_MODEL", "llama3")
        
        # Validate required config
        if not all([jenkins_url, jenkins_user, jenkins_token]):
            self.console.print("[red]❌ Missing Jenkins configuration![/red]")
            self.console.print("Please set these environment variables or create a .env file:")
            self.console.print("  - JENKINS_URL")
            self.console.print("  - JENKINS_USER") 
            self.console.print("  - JENKINS_API_TOKEN")
            return False
        
        # Initialize clients
        self.jenkins = JenkinsClient(jenkins_url, jenkins_user, jenkins_token)
        self.ollama = OllamaClient(ollama_url, ollama_model)
        
        # Test connections
        self.console.print("\n[yellow]🔗 Testing connections...[/yellow]")
        
        if not self.jenkins.test_connection():
            self.console.print(f"[red]❌ Cannot connect to Jenkins at {jenkins_url}[/red]")
            return False
        self.console.print(f"[green]✓ Connected to Jenkins at {jenkins_url}[/green]")
        
        if not self.ollama.test_connection():
            self.console.print(f"[red]❌ Cannot connect to Ollama at {ollama_url}[/red]")
            self.console.print("[yellow]⚠ Running in fallback mode (basic keyword matching)[/yellow]")
        else:
            self.console.print(f"[green]✓ Connected to Ollama ({ollama_model})[/green]")
        
        return True
    
    def display_welcome(self):
        """Show welcome message"""
        welcome_text = """
# 🤖 Jenkins AI Chatbot

I'm your intelligent Jenkins assistant! I can help you:

- **Check job status** - "What's the status of my-job?"
- **List all jobs** - "Show me all jobs"
- **View build logs** - "Get logs for build 42 of my-job"
- **Trigger builds** - "Run the deploy job"

Type **help** for more commands or **exit** to quit.
        """
        self.console.print(Panel(Markdown(welcome_text), border_style="blue"))
    
    def display_job_status(self, status: JobStatus):
        """Display job status in a nice format"""
        # Choose color based on status
        if "SUCCESS" in status.status:
            color = "green"
            emoji = "✅"
        elif "FAILED" in status.status:
            color = "red"
            emoji = "❌"
        elif "BUILDING" in status.status:
            color = "yellow"
            emoji = "🔄"
        elif "UNSTABLE" in status.status:
            color = "yellow"
            emoji = "⚠️"
        else:
            color = "white"
            emoji = "❓"
        
        table = Table(title=f"{emoji} Job: {status.name}", border_style=color)
        table.add_column("Property", style="cyan")
        table.add_column("Value", style=color)
        
        table.add_row("Status", status.status)
        table.add_row("Last Build", f"#{status.last_build_number}" if status.last_build_number else "N/A")
        table.add_row("Last Result", status.last_build_result or "N/A")
        
        if status.last_build_duration:
            duration_sec = status.last_build_duration / 1000
            table.add_row("Duration", f"{duration_sec:.1f} seconds")
        
        if status.health_score is not None:
            health_bar = "█" * (status.health_score // 10) + "░" * (10 - status.health_score // 10)
            table.add_row("Health", f"{health_bar} {status.health_score}%")
        
        table.add_row("URL", status.url)
        
        self.console.print(table)
    
    def display_job_list(self, jobs: list[dict]):
        """Display list of jobs"""
        if not jobs:
            self.console.print("[yellow]No jobs found in Jenkins.[/yellow]")
            return
        
        table = Table(title="📋 Jenkins Jobs", border_style="blue")
        table.add_column("#", style="dim")
        table.add_column("Job Name", style="cyan")
        table.add_column("Status", style="white")
        
        for i, job in enumerate(jobs, 1):
            color = job.get("color", "grey")
            if "blue" in color:
                status = "[green]SUCCESS[/green]"
            elif "red" in color:
                status = "[red]FAILED[/red]"
            elif "yellow" in color:
                status = "[yellow]UNSTABLE[/yellow]"
            elif "anime" in color:
                status = "[yellow]BUILDING[/yellow]"
            elif "disabled" in color:
                status = "[dim]DISABLED[/dim]"
            else:
                status = "[white]UNKNOWN[/white]"
            
            table.add_row(str(i), job.get("name", "Unknown"), status)
        
        self.console.print(table)
    
    def display_help(self):
        """Show help message"""
        help_text = """
## Available Commands

| Command | Description | Example |
|---------|-------------|---------|
| **status** | Check job status | "What's the status of deploy-job?" |
| **list** | Show all jobs | "Show me all jobs" |
| **logs** | View build logs | "Get logs for build 5 of my-job" |
| **trigger** | Start a build | "Run the test-pipeline job" |
| **help** | Show this help | "help" |
| **exit** | Quit chatbot | "exit" or "quit" |

### Tips
- You can use natural language - I'll understand!
- Put job names in quotes if they contain spaces
- For logs, specify build number for specific build
        """
        self.console.print(Panel(Markdown(help_text), title="Help", border_style="green"))
    
    def handle_intent(self, intent_data: dict):
        """Handle the parsed user intent"""
        intent = intent_data.get("intent", "unknown")
        job_name = intent_data.get("job_name")
        build_number = intent_data.get("build_number")
        response = intent_data.get("response", "")
        
        # Show AI response
        if response:
            self.console.print(f"[blue]🤖 {response}[/blue]\n")
        
        if intent == "list_jobs":
            jobs = self.jenkins.get_all_jobs()
            self.display_job_list(jobs)
            
        elif intent == "get_job_status":
            if not job_name:
                # Show all jobs if no specific job mentioned
                jobs = self.jenkins.get_all_jobs()
                if jobs:
                    self.console.print("[yellow]Which job? Here are the available jobs:[/yellow]\n")
                    self.display_job_list(jobs)
                else:
                    self.console.print("[red]No jobs found.[/red]")
            else:
                status = self.jenkins.get_job_status(job_name)
                if status:
                    self.display_job_status(status)
                else:
                    self.console.print(f"[red]Job '{job_name}' not found.[/red]")
                    self.console.print("[yellow]Tip: Use 'list jobs' to see available jobs.[/yellow]")
        
        elif intent == "get_build_log":
            if not job_name:
                self.console.print("[yellow]Please specify a job name for the logs.[/yellow]")
            else:
                log = self.jenkins.get_build_log(job_name, build_number)
                if log:
                    # Truncate if too long
                    max_lines = 50
                    lines = log.split('\n')
                    if len(lines) > max_lines:
                        self.console.print(f"[dim](Showing last {max_lines} lines of {len(lines)} total)[/dim]\n")
                        log = '\n'.join(lines[-max_lines:])
                    
                    self.console.print(Panel(
                        log,
                        title=f"📜 Build Log: {job_name} #{build_number or 'latest'}",
                        border_style="cyan"
                    ))
                else:
                    self.console.print(f"[red]Could not fetch logs for {job_name}.[/red]")
        
        elif intent == "trigger_build":
            if not job_name:
                self.console.print("[yellow]Please specify which job to build.[/yellow]")
            else:
                confirm = Prompt.ask(
                    f"[yellow]⚠ Trigger build for '{job_name}'?[/yellow]",
                    choices=["y", "n"],
                    default="n"
                )
                if confirm.lower() == "y":
                    if self.jenkins.trigger_build(job_name):
                        self.console.print(f"[green]✓ Build triggered for {job_name}![/green]")
                    else:
                        self.console.print(f"[red]Failed to trigger build for {job_name}.[/red]")
                else:
                    self.console.print("[dim]Build cancelled.[/dim]")
        
        elif intent == "help":
            self.display_help()
        
        else:
            self.console.print("[yellow]I'm not sure what you mean. Try 'help' to see what I can do.[/yellow]")
    
    def run(self):
        """Main chat loop"""
        if not self.setup():
            return
        
        self.display_welcome()
        
        while True:
            try:
                # Get user input
                user_input = Prompt.ask("\n[bold green]You[/bold green]").strip()
                
                if not user_input:
                    continue
                
                # Check for exit commands
                if user_input.lower() in ["exit", "quit", "bye", "q"]:
                    self.console.print("[blue]👋 Goodbye! Happy building![/blue]")
                    break
                
                # Parse intent using Ollama
                with self.console.status("[yellow]Thinking...[/yellow]"):
                    intent_data = self.ollama.parse_user_intent(user_input)
                
                # Handle the intent
                self.handle_intent(intent_data)
                
            except KeyboardInterrupt:
                self.console.print("\n[blue]👋 Goodbye![/blue]")
                break
            except Exception as e:
                self.console.print(f"[red]Error: {e}[/red]")


def main():
    """Entry point"""
    chatbot = JenkinsChatbot()
    chatbot.run()


if __name__ == "__main__":
    main()
