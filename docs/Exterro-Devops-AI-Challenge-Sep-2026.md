# EXTERRO · DEVOPS · MONTHLY AI CHALLENGE

## BuildBot — Chat-driven hotfix builds

**September 2026**

| | |
|---|---|
| **ENV** | Local Jenkins |
| **MODEL** | Local Ollama |
| **NOTIFY** | Google Chat + Email |
| **MODE** | Team |

---

## 01 The problem we're solving

Today, when a developer needs a hotfix build:

1. They post the GitHub URL and branch name in chat.
2. Someone from our team has to notice it, be available, and manually trigger the Jenkins job.
3. That person watches the job, then manually posts "done" back in Google Chat.
4. If the dev only needs specific DLLs, someone manually copies them to the shared path and shares the location.

The cost: a human is blocked on every request, and turnaround is worst exactly when it hurts most — nights, weekends, holidays.

**Your mission this month:** build a working prototype that removes the human from steps 2 to 4.

---

## 02 What you're building

A chat window where a developer types a plain-English build request, an AI layer understands it, triggers the right Jenkins job, waits for it to finish, and reports the result back — in the chat window, in Google Chat, and by email.

### Example interaction

**DEV**
> need a hotfix build from https://github.com/acme/payments-api, branch hotfix/PAY-4821

**BUILDBOT**
> Got it — triggering `hotfix-build` for `hotfix/PAY-4821`. Build #143 queued. I'll update you here.

*… a few minutes later …*

**BUILDBOT**
> Build #143 SUCCESS (2m 14s). Artifacts: `\\shared\builds\143\`. Notification posted to GChat.

---

## 03 Ground rules

| RULE | WHY |
|---|---|
| Do not touch the real Jenkins server. Install Jenkins locally on your own Windows laptop. | This is a sandbox. No production risk. |
| This is a team activity — not an individual one. Form teams among yourselves, any size you want. Mix experience levels: put someone new to AI alongside someone new to Jenkins. | Nobody should be working on this alone, and the learning is in the discussion. |
| No company source code. Use a public sample repo or a throwaway repo you create. | Keeps it shareable and demo-able. |
| No real credentials anywhere in the repo. Use `.env` or the Jenkins credentials store. | Basic hygiene. |
| Use our local Ollama model on the GCP box. No external LLM APIs. | Cost and data policy. |

---

## 04 Levels — build in this order

You are done and successful at Level 1. Everything after is bonus.

### LEVEL 1 / MUST HAVE

This is "passing". A simple thing that works reliably beats a clever thing that demos badly.

- Jenkins running locally, with one parameterised job that accepts `GITHUB_URL` and `BRANCH` and produces some build output.
- A simple chat UI — one input box, one message list. Streamlit, Gradio, or a plain HTML + Flask page. Do not spend time on beauty.
- The message goes to Ollama, which extracts the repo URL and branch into structured fields (JSON).
- Your app calls the Jenkins REST API to trigger the job with those parameters.
- Your app polls until the build finishes and shows the result in the chat window.
- On completion, post a message to the Google Chat webhook.

### LEVEL 2 / SHOULD HAVE

- Live status in the chat while it runs — queued, building, finished — not a frozen screen.
- Email notification on completion (see the tip in section 06 — do not fight corporate SMTP).
- Graceful handling of the unhappy paths: branch doesn't exist, Jenkins is down, build fails, or the dev's message is missing the branch. The bot should ask rather than guess.
- Show the last 20 lines of console output when a build fails.
- Copy build artifacts to a "shared path" (a local folder is fine) and return the location.
- Handle "just give me `Payments.Core.dll` and `Payments.Api.dll`" — pick out only the requested files and stage them.

### LEVEL 3 / STRETCH

- Multiple jobs — the bot decides which Jenkins job fits the request.
- Conversation memory: "rebuild that same branch", "what was the status of my last build?"
- Make Google Chat itself the front end, with no separate UI.
- Tool-calling or MCP-style design: instead of one big prompt, give the model tools (`trigger_build`, `get_build_status`, `list_jobs`) and let it choose.
- An approval gate — the bot proposes the action and waits for a thumbs-up before triggering.

---

## 05 Setup we're providing

| THING | DETAIL |
|---|---|
| Ollama endpoint | `http://<GCP-IP>:11434` — model: `<model-name>` |
| Google Chat space | "DevOps AI Challenge" — incoming webhook URL shared privately |
| Sample repo to build | `<public sample .NET repo>`, or create your own trivial one |
| Jenkins | You install locally — steps below |

### Jenkins on your laptop — about 15 minutes

1. Install Java 17+, then download Jenkins LTS for Windows (installer or `jenkins.war`).
2. Run `java -jar jenkins.war --httpPort=8080` and open `http://localhost:8080`.
3. Unlock with the initial admin password it prints, then install the suggested plugins.
4. Create a Freestyle job, tick "This project is parameterised", and add String parameters `GITHUB_URL` and `BRANCH`.
5. Under **Manage Jenkins → Users → your user → Security**, generate an API token. You'll use `username:apitoken` for API calls.

---

## 06 Hints so you don't get stuck

### Triggering a Jenkins build via REST — the part that trips everyone up

- Trigger: `POST http://localhost:8080/job/<JOB_NAME>/buildWithParameters?GITHUB_URL=...&BRANCH=...` with HTTP Basic auth (user + API token).
- The response body is empty. The useful bit is the `Location` response header, which is a queue URL, not a build URL.
- Poll `GET <queue_url>/api/json` until an `executable` object appears. That gives you the real build number.
- Then poll `GET .../job/<JOB_NAME>/<number>/api/json` and watch `building` (true/false) and `result` (`SUCCESS`, `FAILURE`, `ABORTED`).
- Console log: `GET .../<number>/consoleText`.
- If you hit `403 "No valid crumb"`, use the API token via Basic auth rather than a password. That's the usual fix.

### Getting reliable structure out of the LLM

- Ask for JSON only, give it an example, and set temperature near 0. For instance:
  `Return only JSON: {"repo_url":..., "branch":..., "artifacts": [...], "missing":[...]}`. No prose, no markdown fences.
- Always validate the output in code. Strip stray backticks, parse inside a try/except, and re-prompt once if it fails.
- Never let the model invent a repo URL or job name. Check the extracted repo against an allowlist you control. The model's job is to understand the request, not to decide what's safe to build.
- If a required field is missing, have the bot ask the dev. Don't guess a default branch.

### Email and Google Chat without the pain

- Install Papercut SMTP or MailHog locally — a fake SMTP server with a web inbox. Point your app at `localhost:25` and you have working email in five minutes with zero approvals.
- The Google Chat webhook is just a POST of `{"text": "your message"}` to the webhook URL. Start there. Cards can come later.

---

## 07 Deliverables

1. Git repo with your code and a README a teammate could follow to run it.
2. Architecture diagram — a whiteboard photo is completely fine.
3. Live demo, 10 minutes max: type a request, build runs, notification lands. A recorded video is an acceptable backup if your laptop misbehaves.
4. One page of reflection: What did the AI genuinely help with? Where did it get in the way? Which prompt version finally worked, and why? What would you do differently for a real production version?
5. Your final prompt(s), pasted in the README.

---

*EXTERRO · DEVOPS AI CHALLENGE · SEPTEMBER 2026*
