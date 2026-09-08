"""
Google Chat Card Builder
Creates rich card responses for Google Chat
"""

from typing import Optional
from jenkins_client import JobStatus


class ChatCardBuilder:
    """Builds Google Chat card responses"""
    
    def build_job_list_card(self, jobs: list[dict]) -> dict:
        """Build a card showing list of all jobs"""
        if not jobs:
            return {"text": "📋 No jobs found in Jenkins."}
        
        # Build widgets for each job
        widgets = []
        
        for job in jobs[:15]:  # Limit to 15 jobs to avoid huge cards
            name = job.get("name", "Unknown")
            color = job.get("color", "grey")
            
            # Map color to emoji and status
            if "blue" in color:
                emoji, status = "✅", "SUCCESS"
            elif "red" in color:
                emoji, status = "❌", "FAILED"
            elif "yellow" in color:
                emoji, status = "⚠️", "UNSTABLE"
            elif "anime" in color:
                emoji, status = "🔄", "BUILDING"
            elif "disabled" in color:
                emoji, status = "⏸️", "DISABLED"
            else:
                emoji, status = "❓", "UNKNOWN"
            
            widgets.append({
                "decoratedText": {
                    "topLabel": status,
                    "text": f"{emoji} {name}",
                    "button": {
                        "text": "Status",
                        "onClick": {
                            "action": {
                                "actionMethodName": "get_status",
                                "parameters": [{"key": "job_name", "value": name}]
                            }
                        }
                    }
                }
            })
        
        if len(jobs) > 15:
            widgets.append({
                "textParagraph": {
                    "text": f"<i>...and {len(jobs) - 15} more jobs</i>"
                }
            })
        
        return {
            "cardsV2": [{
                "cardId": "job-list",
                "card": {
                    "header": {
                        "title": "📋 Jenkins Jobs",
                        "subtitle": f"{len(jobs)} jobs found"
                    },
                    "sections": [{
                        "widgets": widgets
                    }]
                }
            }]
        }
    
    def build_job_status_card(self, status: JobStatus) -> dict:
        """Build a detailed status card for a single job"""
        # Choose color/emoji based on status
        if "SUCCESS" in status.status:
            color = "#34A853"  # Green
            emoji = "✅"
        elif "FAILED" in status.status:
            color = "#EA4335"  # Red
            emoji = "❌"
        elif "BUILDING" in status.status:
            color = "#FBBC04"  # Yellow
            emoji = "🔄"
        elif "UNSTABLE" in status.status:
            color = "#FA7B17"  # Orange
            emoji = "⚠️"
        else:
            color = "#9E9E9E"  # Grey
            emoji = "❓"
        
        # Build info widgets
        widgets = [
            {
                "decoratedText": {
                    "topLabel": "Status",
                    "text": f"{emoji} <b>{status.status}</b>"
                }
            },
            {
                "decoratedText": {
                    "topLabel": "Last Build",
                    "text": f"#{status.last_build_number}" if status.last_build_number else "No builds yet"
                }
            }
        ]
        
        if status.last_build_result:
            widgets.append({
                "decoratedText": {
                    "topLabel": "Last Result",
                    "text": status.last_build_result
                }
            })
        
        if status.last_build_duration:
            duration_sec = status.last_build_duration / 1000
            if duration_sec < 60:
                duration_str = f"{duration_sec:.0f} seconds"
            else:
                duration_str = f"{duration_sec / 60:.1f} minutes"
            widgets.append({
                "decoratedText": {
                    "topLabel": "Duration",
                    "text": duration_str
                }
            })
        
        if status.health_score is not None:
            health_bar = "🟢" * (status.health_score // 20) + "⚪" * (5 - status.health_score // 20)
            widgets.append({
                "decoratedText": {
                    "topLabel": "Health",
                    "text": f"{health_bar} {status.health_score}%"
                }
            })
        
        # Add action buttons
        buttons = {
            "buttonList": {
                "buttons": [
                    {
                        "text": "🔄 Refresh",
                        "onClick": {
                            "action": {
                                "actionMethodName": "refresh_status",
                                "parameters": [{"key": "job_name", "value": status.name}]
                            }
                        }
                    },
                    {
                        "text": "📜 Logs",
                        "onClick": {
                            "action": {
                                "actionMethodName": "get_logs",
                                "parameters": [{"key": "job_name", "value": status.name}]
                            }
                        }
                    },
                    {
                        "text": "🚀 Build",
                        "onClick": {
                            "action": {
                                "actionMethodName": "trigger_build",
                                "parameters": [{"key": "job_name", "value": status.name}]
                            }
                        }
                    }
                ]
            }
        }
        
        widgets.append(buttons)
        
        # Add link to Jenkins
        widgets.append({
            "buttonList": {
                "buttons": [{
                    "text": "🔗 Open in Jenkins",
                    "onClick": {
                        "openLink": {
                            "url": status.url
                        }
                    }
                }]
            }
        })
        
        return {
            "cardsV2": [{
                "cardId": f"job-status-{status.name}",
                "card": {
                    "header": {
                        "title": f"🔧 {status.name}",
                        "subtitle": "Job Status"
                    },
                    "sections": [{
                        "widgets": widgets
                    }]
                }
            }]
        }
    
    def build_trigger_confirmation_card(self, job_name: str) -> dict:
        """Build a confirmation card for triggering a build"""
        return {
            "cardsV2": [{
                "cardId": f"trigger-confirm-{job_name}",
                "card": {
                    "header": {
                        "title": "🚀 Trigger Build?",
                        "subtitle": job_name
                    },
                    "sections": [{
                        "widgets": [
                            {
                                "textParagraph": {
                                    "text": f"Are you sure you want to trigger a new build for <b>{job_name}</b>?"
                                }
                            },
                            {
                                "buttonList": {
                                    "buttons": [
                                        {
                                            "text": "✅ Yes, Build",
                                            "color": {
                                                "red": 0.2,
                                                "green": 0.65,
                                                "blue": 0.32,
                                                "alpha": 1
                                            },
                                            "onClick": {
                                                "action": {
                                                    "actionMethodName": "trigger_build",
                                                    "parameters": [{"key": "job_name", "value": job_name}]
                                                }
                                            }
                                        },
                                        {
                                            "text": "❌ Cancel",
                                            "onClick": {
                                                "action": {
                                                    "actionMethodName": "cancel_build",
                                                    "parameters": []
                                                }
                                            }
                                        }
                                    ]
                                }
                            }
                        ]
                    }]
                }
            }]
        }
    
    def build_help_card(self) -> dict:
        """Build a help card showing available commands"""
        return {
            "cardsV2": [{
                "cardId": "help",
                "card": {
                    "header": {
                        "title": "🤖 Jenkins Bot Help",
                        "subtitle": "What I can do for you"
                    },
                    "sections": [
                        {
                            "header": "📋 Job Information",
                            "widgets": [
                                {
                                    "decoratedText": {
                                        "topLabel": "List Jobs",
                                        "text": "\"show me all jobs\" or \"list jobs\""
                                    }
                                },
                                {
                                    "decoratedText": {
                                        "topLabel": "Job Status",
                                        "text": "\"status of my-job\" or \"how is deploy doing?\""
                                    }
                                },
                                {
                                    "decoratedText": {
                                        "topLabel": "Build Logs",
                                        "text": "\"logs for my-job\" or \"show output of build 42\""
                                    }
                                }
                            ]
                        },
                        {
                            "header": "🚀 Actions",
                            "widgets": [
                                {
                                    "decoratedText": {
                                        "topLabel": "Trigger Build",
                                        "text": "\"run my-job\" or \"build the deploy pipeline\""
                                    }
                                }
                            ]
                        },
                        {
                            "header": "💡 Tips",
                            "widgets": [
                                {
                                    "textParagraph": {
                                        "text": "• I understand natural language - just ask!\n• Job names are case-sensitive\n• Click buttons on cards for quick actions"
                                    }
                                }
                            ]
                        }
                    ]
                }
            }]
        }
    
    def build_error_card(self, title: str, message: str) -> dict:
        """Build an error card"""
        return {
            "cardsV2": [{
                "cardId": "error",
                "card": {
                    "header": {
                        "title": f"❌ {title}",
                        "subtitle": "Error"
                    },
                    "sections": [{
                        "widgets": [{
                            "textParagraph": {
                                "text": message
                            }
                        }]
                    }]
                }
            }]
        }
