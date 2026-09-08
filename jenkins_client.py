"""
Jenkins API Client Module
Handles all communication with Jenkins server
"""

import requests
from requests.auth import HTTPBasicAuth
from typing import Optional
from dataclasses import dataclass


@dataclass
class JobStatus:
    """Represents the status of a Jenkins job"""
    name: str
    status: str
    last_build_number: Optional[int]
    last_build_result: Optional[str]
    last_build_duration: Optional[int]
    is_building: bool
    url: str
    health_score: Optional[int]


class JenkinsClient:
    """Client for interacting with Jenkins API"""
    
    def __init__(self, url: str, username: str, api_token: str):
        """
        Initialize Jenkins client
        
        Args:
            url: Jenkins server URL (e.g., http://localhost:8080)
            username: Jenkins username
            api_token: Jenkins API token (generate from user settings)
        """
        self.url = url.rstrip('/')
        self.auth = HTTPBasicAuth(username, api_token)
        self.session = requests.Session()
        self.session.auth = self.auth
    
    def test_connection(self) -> bool:
        """Test if connection to Jenkins is working"""
        try:
            response = self.session.get(f"{self.url}/api/json", timeout=10)
            return response.status_code == 200
        except requests.RequestException:
            return False
    
    def get_all_jobs(self) -> list[dict]:
        """Get list of all jobs"""
        try:
            response = self.session.get(
                f"{self.url}/api/json",
                params={"tree": "jobs[name,url,color]"},
                timeout=10
            )
            response.raise_for_status()
            data = response.json()
            return data.get("jobs", [])
        except requests.RequestException as e:
            print(f"Error fetching jobs: {e}")
            return []
    
    def get_job_status(self, job_name: str) -> Optional[JobStatus]:
        """
        Get detailed status of a specific job
        
        Args:
            job_name: Name of the Jenkins job
            
        Returns:
            JobStatus object or None if job not found
        """
        try:
            # Get job info
            job_url = f"{self.url}/job/{job_name}/api/json"
            response = self.session.get(job_url, timeout=10)
            
            if response.status_code == 404:
                return None
                
            response.raise_for_status()
            job_data = response.json()
            
            # Parse build status
            last_build = job_data.get("lastBuild")
            last_build_number = last_build.get("number") if last_build else None
            
            # Get last build details if available
            last_build_result = None
            last_build_duration = None
            
            if last_build_number:
                build_url = f"{self.url}/job/{job_name}/{last_build_number}/api/json"
                build_response = self.session.get(build_url, timeout=10)
                if build_response.status_code == 200:
                    build_data = build_response.json()
                    last_build_result = build_data.get("result")
                    last_build_duration = build_data.get("duration")
            
            # Determine status from color
            color = job_data.get("color", "notbuilt")
            status = self._color_to_status(color)
            
            # Get health score
            health_report = job_data.get("healthReport", [])
            health_score = health_report[0].get("score") if health_report else None
            
            return JobStatus(
                name=job_name,
                status=status,
                last_build_number=last_build_number,
                last_build_result=last_build_result,
                last_build_duration=last_build_duration,
                is_building=color.endswith("_anime") if color else False,
                url=job_data.get("url", ""),
                health_score=health_score
            )
            
        except requests.RequestException as e:
            print(f"Error fetching job status: {e}")
            return None
    
    def get_build_log(self, job_name: str, build_number: int = None) -> Optional[str]:
        """
        Get console output for a build
        
        Args:
            job_name: Name of the Jenkins job
            build_number: Build number (defaults to last build)
            
        Returns:
            Console output as string or None
        """
        try:
            if build_number is None:
                build_number = "lastBuild"
            
            log_url = f"{self.url}/job/{job_name}/{build_number}/consoleText"
            response = self.session.get(log_url, timeout=30)
            
            if response.status_code == 200:
                return response.text
            return None
            
        except requests.RequestException as e:
            print(f"Error fetching build log: {e}")
            return None
    
    def trigger_build(self, job_name: str) -> bool:
        """
        Trigger a new build for a job
        
        Args:
            job_name: Name of the Jenkins job
            
        Returns:
            True if build was triggered successfully
        """
        try:
            build_url = f"{self.url}/job/{job_name}/build"
            response = self.session.post(build_url, timeout=10)
            return response.status_code in [200, 201, 302]
        except requests.RequestException as e:
            print(f"Error triggering build: {e}")
            return False
    
    def _color_to_status(self, color: str) -> str:
        """Convert Jenkins color to human-readable status"""
        color_map = {
            "blue": "SUCCESS",
            "blue_anime": "BUILDING (was SUCCESS)",
            "red": "FAILED",
            "red_anime": "BUILDING (was FAILED)",
            "yellow": "UNSTABLE",
            "yellow_anime": "BUILDING (was UNSTABLE)",
            "grey": "PENDING",
            "grey_anime": "BUILDING (was PENDING)",
            "disabled": "DISABLED",
            "aborted": "ABORTED",
            "aborted_anime": "BUILDING (was ABORTED)",
            "notbuilt": "NOT BUILT",
            "notbuilt_anime": "BUILDING (first build)"
        }
        return color_map.get(color, f"UNKNOWN ({color})")
