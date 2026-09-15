#!/usr/bin/env python3
import ast, json, os, re, sys
from datetime import datetime
from urllib.parse import urlparse
import requests
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

RED, GREEN, YELLOW, BLUE, RESET = "\033[91m", "\033[92m", "\033[93m", "\033[94m", "\033[0m"
CDP_URL, OLLAMA_URL, MODEL = "http://127.0.0.1:9223", "http://127.0.0.1:11434/api/chat", "qwen3:4b-instruct"
ROOT_DIR = os.path.dirname(os.path.abspath(__file__))
LEARNED_DIR, EVIDENCE_DIR = os.path.join(ROOT_DIR, "learned"), os.path.join(ROOT_DIR, "evidence")
ALLOWED_DOMAINS, ALLOWED_ACTIONS = {"mail.google.com", "youtube.com"}, {"click", "fill", "press"}
ALLOWED_LOCATORS, CONFIRM_ACTIONS = ("page.locator(", "page.get_by_text("), {"send", "delete"}
SENSITIVE_RE = re.compile(r"\b\d{6,}\b|\b[A-Za-z0-9_-]{24,}\b")
SECRET_FIELD_RE = re.compile(r"password|passcode|otp|one[- ]?time|token|secret|api[-_ ]?key", re.I)
SECRET_VALUE_RE = re.compile(r"\b(password|passcode|otp|one[- ]?time(?: code)?|token|secret|api[-_ ]?key)(\s*(?:is|=|:)?\s*)(\S+)", re.I)

ENUMERATE_ACTIONS = r"""() => {
const actions=["page.keyboard.press('Enter')"],quote=v=>"'"+v.replace(/\\/g,"\\\\").replace(/'/g,"\\'")+"'";
for(const e of document.querySelectorAll("button,a,input,textarea,[contenteditable='true'],[role='button'],[role='link'],[role='checkbox'],[role='radio'],[role='switch']")){
const r=e.getBoundingClientRect(),s=getComputedStyle(e);if(!r.width||!r.height||s.display==="none"||s.visibility==="hidden")continue;
const aria=(e.getAttribute("aria-label")||"").trim(),name=(e.getAttribute("name")||"").trim(),placeholder=(e.getAttribute("placeholder")||"").trim(),text=(e.innerText||e.textContent||e.value||"").trim().replace(/\s+/g," ").slice(0,80),tag=e.tagName.toLowerCase(),role=e.getAttribute("role"),type=(e.getAttribute("type")||"").toLowerCase();
if(!aria&&!name&&!placeholder&&!text)continue;
const locator=aria?`page.locator(${quote("[aria-label="+JSON.stringify(aria)+"]:visible")}).first`:name?`page.locator(${quote("[name="+JSON.stringify(name)+"]:visible")}).first`:placeholder?`page.locator(${quote("[placeholder="+JSON.stringify(placeholder)+"]:visible")}).first`:`page.get_by_text(${quote(text)}, exact=True).first`;
if(tag==="textarea"||e.isContentEditable||(tag==="input"&&!["hidden","button","submit","reset","checkbox","radio"].includes(type)))actions.push(locator+".fill");
if(["button","a"].includes(tag)||["button","link","checkbox","radio","switch"].includes(role)||["checkbox","radio"].includes(type))actions.push(locator+".click");
}return actions;}"""

RECORDER = r"""(() => {
if(window.__caRecorder)return;window.__caRecorder=true;
const label=e=>(e.getAttribute?.("aria-label")||e.getAttribute?.("name")||e.getAttribute?.("placeholder")||e.tagName||"unknown").trim().replace(/\s+/g," ").slice(0,80);
const record=a=>{if(sessionStorage.__caOn!=="1")return;const x=JSON.parse(sessionStorage.__caActions||"[]");x.push(a);sessionStorage.__caActions=JSON.stringify(x.slice(-100));};
addEventListener("click",e=>record({type:"click",target:label(e.target)}),true);
addEventListener("input",e=>record({type:"input",target:label(e.target),value:"<redacted>"}),true);
addEventListener("keydown",e=>{if(["Enter","Escape","Tab"].includes(e.key))record({type:"press",key:e.key,target:label(e.target)})},true);
})()"""

def redact_text(value):
    parsed = urlparse(value)
    if parsed.scheme in {"http", "https"} and parsed.netloc:
        return value
    return SENSITIVE_RE.sub("<redacted>", SECRET_VALUE_RE.sub(lambda m: m.group(1) + m.group(2) + "<redacted>", value))

def redact(value):
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, list):
        return [redact(x) for x in value]
    if isinstance(value, dict):
        return {k: "<redacted>" if SECRET_FIELD_RE.search(k) else redact(v) for k, v in value.items()}
    return value

def redact_action(action):
    if ".fill(" in action and SECRET_FIELD_RE.search(action):
        return action.rsplit(".fill(", 1)[0] + ".fill('<redacted>')"
    return redact_text(action)

def log_event(run_log, event):
    with open(run_log, "a") as file:
        file.write(json.dumps(redact(event)) + "\n")

def validate_domain(page):
    host = (urlparse(page.url).hostname or "").removeprefix("www.")
    if host not in ALLOWED_DOMAINS:
        raise RuntimeError(f"Domain {host} is not allowed.")

def execute_action(page, action):
    if action == "page.keyboard.press('Enter')":
        if "press" not in ALLOWED_ACTIONS:
            raise RuntimeError(f"Action not allowed: {action}")
        return page.keyboard.press("Enter")
    if ".fill(" in action and action.endswith(")"):
        if "fill" not in ALLOWED_ACTIONS:
            raise RuntimeError(f"Action not allowed: {action}")
        call, value = action.rsplit(".fill(", 1)
        if not call.startswith(ALLOWED_LOCATORS):
            raise RuntimeError(f"Locator not allowed: {action}")
        return eval(call + ".fill", {"__builtins__": {}, "page": page})(ast.literal_eval(value[:-1]))
    if action.endswith(".click"):
        if "click" not in ALLOWED_ACTIONS or not action.startswith(ALLOWED_LOCATORS):
            raise RuntimeError(f"Action not allowed: {action}")
        return eval(action, {"__builtins__": {}, "page": page})()
    raise RuntimeError(f"Action not allowed: {action}")

def approve_action(action, run_log):
    risk = next((x for x in CONFIRM_ACTIONS if x in action.lower()), None) if action.endswith(".click") else None
    if not risk:
        return True
    approved = input(f"{RED}Risky action ({risk}) requires approval. Execute? [y/N]: {RESET}").strip().lower() == "y"
    log_event(run_log, {"event": "risky_action_approval", "action": redact_action(action), "approved": approved})
    return approved

def match_artifact(goal, discoveries):
    for template, artifact in discoveries.items():
        pattern = re.escape(template)
        for name in artifact.get("inputs", {}):
            pattern = pattern.replace(re.escape("{" + name + "}"), f"(?P<{name}>.+?)")
        match = re.fullmatch(pattern, goal, re.I)
        if match:
            return artifact, match.groupdict()
    return None, {}

def materialize_actions(artifact, params):
    actions = []
    for saved in artifact["actions"]:
        action = saved
        for name, value in params.items():
            action = action.replace("{" + name + "}", json.dumps(value))
        actions.append(action)
    return actions

def checkpoint_for(goal, site):
    key = goal.lower()
    if site == "youtube" and "search" in key:
        return "youtube_search_results", {"links": "list[string]"}
    if "send an email" in key:
        return "gmail_message_sent", {"sent": "boolean"}
    if "search" in key and "email" in key:
        return "gmail_search_results", {"emails": "list[string]"}
    return "actions_completed", {}

def get_result(page, checkpoint):
    if checkpoint == "youtube_search_results":
        links = page.locator("a#video-title").evaluate_all("x=>x.map(a=>a.href).filter(Boolean)")
        return {"status": "success", "outputs": {"links": links}} if links else {"status": "business_outcome", "outcome": "no_results", "outputs": {"links": []}}
    if checkpoint == "gmail_message_sent":
        return {"status": "success", "outputs": {"sent": True}} if page.get_by_text("Message sent", exact=False).count() else {"status": "failure", "expected": "Message sent confirmation", "observed": "Confirmation not found"}
    if checkpoint == "gmail_search_results":
        if "#search/" not in page.url:
            return {"status": "failure", "expected": "Gmail search results", "observed": page.url}
        emails = page.locator("tr.zA").all_inner_texts()
        return {"status": "success", "outputs": {"emails": emails}} if emails else {"status": "business_outcome", "outcome": "no_results", "outputs": {"emails": []}}
    return {"status": "success", "outputs": {}}

def build_capability(goal, actions, host, site):
    template, inputs, saved_actions = goal, {}, []
    for action in actions:
        saved = action
        if ".fill(" in action and action.endswith(")"):
            call, raw = action.rsplit(".fill(", 1)
            value = ast.literal_eval(raw[:-1])
            match = re.search(re.escape(value), template, re.I) if value else None
            if match:
                label = re.search(r'\[(?:aria-label|name|placeholder)="([^"]+)"', call)
                base = re.sub(r"\W+", "_", (label.group(1) if label else "input").lower()).strip("_") or "input"
                name, n = base, 2
                while name in inputs:
                    name, n = f"{base}_{n}", n + 1
                template = template[:match.start()] + "{" + name + "}" + template[match.end():]
                inputs[name] = "string"
                saved = call + ".fill({" + name + "})"
            elif SECRET_FIELD_RE.search(call) or redact_text(value) != value:
                raise RuntimeError("Sensitive fill value must be supplied as a runtime parameter.")
        if redact_action(saved) != saved:
            raise RuntimeError("Sensitive data would remain in the saved artifact.")
        saved_actions.append(saved)
    if redact_text(template) != template:
        raise RuntimeError("Sensitive data would remain in the goal template.")
    checkpoint, outputs = checkpoint_for(goal, site)
    return template, {"version": 1, "goal": template, "target": host, "inputs": inputs, "actions": saved_actions, "checkpoint": checkpoint, "outputs": outputs}

def propose_actions(page, goal, learned, actions, feedback):
    prompt = f"""You control a web interface.

CURRENT URL:
{page.url}

PAGE TITLE:
{page.title()}

PREVIOUS LEARNED CAPABILITIES:
{learned}

GOAL:
{goal}

AVAILABLE ACTIONS:
{chr(10).join(page.evaluate(ENUMERATE_ACTIONS))}

ACTIONS ALREADY EXECUTED:
{chr(10).join(actions) if actions else "(none)"}

HUMAN FEEDBACK:
{chr(10).join(feedback) if feedback else "(none)"}

Return one or more Playwright actions in execution order.

Rules:
- You must complete the goal with the minimum number of actions from AVAILABLE ACTIONS.
- For a .fill action only, append one positional string argument containing the text to type.
- Follow HUMAN FEEDBACK.
- Do NOT repeat the previous executed action.
- Return one Playwright action per line."""
    response = requests.post(OLLAMA_URL, json={"model": MODEL, "stream": False, "options": {"num_ctx": 16384}, "messages": [{"role": "user", "content": prompt}]}, timeout=60)
    response.raise_for_status()
    result = response.json()["message"]["content"].strip()
    if not result:
        raise RuntimeError("Ollama returned no action.")
    return result, [x.strip() for x in result.splitlines() if x.strip()]

def save_error_evidence(page, mode, run_id, run_log):
    text = page.locator("body").inner_text()
    values = page.locator("input, textarea, [contenteditable='true']").evaluate_all("x=>x.map(e=>e.value||e.innerText||'').join('\\n')")
    raw = text + "\n" + values
    safe = redact_text(raw)
    if safe != raw:
        name = f"{mode}_error_{run_id}.txt"
        with open(os.path.join(EVIDENCE_DIR, name), "w") as file:
            file.write(safe)
    else:
        name = f"{mode}_error_{run_id}.png"
        page.screenshot(path=os.path.join(EVIDENCE_DIR, name))
    log_event(run_log, {"event": "evidence", "type": "failure_snapshot", "file": name})

def handoff_to_human(page, goal, action, error, run_log):
    page.evaluate("""() => {sessionStorage.__caActions="[]";sessionStorage.__caOn="1";}""")
    log_event(run_log, {"event": "handoff", "control": "automation_to_human", "failed_action": redact_action(action), "error": str(error)})
    print(f"{RED}Replay step failed: {redact_text(str(error))}{RESET}\n{YELLOW}Control transferred to human.{RESET}")
    if input(f"{YELLOW}Complete the failed step and press Enter, or q to abort: {RESET}").strip().lower() == "q":
        page.evaluate("""() => {sessionStorage.__caOn="0";}""")
        log_event(run_log, {"event": "handoff", "control": "human_abort"})
        return False
    validate_domain(page)
    manual = page.evaluate("""() => {sessionStorage.__caOn="0";return JSON.parse(sessionStorage.__caActions||"[]");}""")
    log_event(run_log, {"event": "human_actions", "actions": manual})
    log_event(run_log, {"event": "handoff", "control": "human_to_automation"})
    return True

def main():
    if len(sys.argv) < 2:
        print(f'{RED}Usage: computer-agent "goal"{RESET}')
        sys.exit(1)
    goal = " ".join(sys.argv[1:])
    print(f"{GREEN}Goal:{RESET}", redact_text(goal))
    with sync_playwright() as playwright:
        browser = playwright.chromium.connect_over_cdp(CDP_URL)
        if not browser.contexts:
            raise RuntimeError("No Chrome context found.")
        page = next((p for p in reversed(browser.contexts[0].pages) if p.url.startswith(("http://", "https://"))), None)
        if not page:
            raise RuntimeError("No website is open.")
        validate_domain(page)
        page.bring_to_front()
        page.set_default_timeout(2000)
        host = (urlparse(page.url).hostname or "").removeprefix("www.")
        site = host.rsplit(".", 1)[0]
        os.makedirs(LEARNED_DIR, exist_ok=True)
        os.makedirs(EVIDENCE_DIR, exist_ok=True)
        learned_file = os.path.join(LEARNED_DIR, site + ".json")
        discoveries = json.load(open(learned_file)) if os.path.exists(learned_file) else {}
        artifact, params = match_artifact(goal, discoveries)
        replaying = artifact is not None
        mode = "replay" if replaying else "discovery"
        run_id = datetime.now().strftime("%Y%m%d_%H%M%S")
        run_log = os.path.join(EVIDENCE_DIR, f"run_{run_id}.jsonl")
        log_event(run_log, {"event": "start", "mode": mode, "goal": goal, "url": page.url})

        if replaying:
            browser.contexts[0].add_init_script(RECORDER)
            page.evaluate(RECORDER)
            actions = materialize_actions(artifact, params)
            checkpoint = artifact["checkpoint"]
            print(f"{GREEN}Replaying {len(actions)} learned actions...{RESET}")
        else:
            actions, feedback, rounds = [], [], 0
            learned = "\n\n".join(f"Goal: {template}\n" + "\n".join(cap["actions"]) for template, cap in discoveries.items()) if discoveries else "(none)"
            print(f"{YELLOW}Learning capability...{RESET}")

        while True:
            if replaying:
                proposed = actions
            else:
                rounds += 1
                if rounds > 10:
                    raise RuntimeError("Maximum discovery rounds reached.")
                result, proposed = propose_actions(page, goal, learned, actions, feedback)
                log_event(run_log, {"event": "llm_proposal", "actions": [redact_action(x) for x in proposed]})
                print(f"{BLUE}Ollama: {RESET}" + redact_text(result).replace("\n", "\n" + " " * len("Ollama: ")))
                human = input(f"{YELLOW}Enter to approve, s to approve and stop, or type feedback: {RESET}").strip()
                if human and human.lower() != "s":
                    feedback.append(human)
                    log_event(run_log, {"event": "human_feedback", "feedback": human})
                    continue
                log_event(run_log, {"event": "human_approval", "approved": True, "stop_after_batch": human.lower() == "s"})

            for step, action in enumerate(proposed, 1):
                print(f"{BLUE}Action:{RESET}", redact_action(action))
                if not approve_action(action, run_log):
                    result = {"status": "failure", "step": step, "expected": "Human approval", "observed": "Risky action denied"}
                    log_event(run_log, {"event": "complete", "result": result})
                    print(json.dumps(result, indent=2))
                    return
                source = "artifact" if replaying else "llm"
                try:
                    validate_domain(page)
                    if replaying:
                        try:
                            execute_action(page, action)
                        except PlaywrightTimeoutError:
                            log_event(run_log, {"event": "timeout", "step": step, "action": redact_action(action), "response": "retry_once"})
                            print(f"{YELLOW}Action timed out. Retrying once...{RESET}")
                            execute_action(page, action)
                            source = "retry_after_timeout"
                    else:
                        execute_action(page, action)
                    validate_domain(page)
                except Exception as error:
                    log_event(run_log, {"event": "error", "step": step, "action": redact_action(action), "error": str(error)})
                    save_error_evidence(page, mode, run_id, run_log)
                    if replaying:
                        if not handoff_to_human(page, goal, action, error, run_log):
                            result = redact({"status": "failure", "step": step, "expected": "Action to complete", "observed": str(error)})
                            log_event(run_log, {"event": "complete", "result": result})
                            print(json.dumps(result, indent=2))
                            return
                        print(f"{YELLOW}Control returned to replay.{RESET}")
                        continue
                    print(f"{YELLOW}Action failed. Replanning...{RESET}")
                    break
                log_event(run_log, {"event": "action", "step": step, "action": redact_action(action), "source": source, "status": "success"})
                if not replaying:
                    actions.append(action)

            else:
                if replaying:
                    result = redact(get_result(page, checkpoint))
                    log_event(run_log, {"event": "complete", "result": result})
                    print(json.dumps(result, indent=2))
                    return

                if human.lower() == "s":
                    template, capability = build_capability(goal, actions, host, site)
                    result = redact(get_result(page, capability["checkpoint"]))
                    if result["status"] == "failure":
                        log_event(run_log, {"event": "complete", "result": result})
                        print(json.dumps(result, indent=2))
                        return
                    discoveries[template] = capability
                    with open(learned_file, "w") as file:
                        json.dump(discoveries, file, indent=2)
                    artifact_file = os.path.join(EVIDENCE_DIR, f"artifact_{run_id}.json")
                    with open(artifact_file, "w") as file:
                        json.dump(capability, file, indent=2)
                    log_event(run_log, {"event": "artifact_saved", "file": os.path.basename(artifact_file), "checkpoint": capability["checkpoint"], "outputs": capability["outputs"]})
                    log_event(run_log, {"event": "complete", "result": result})
                    print(f"{GREEN}Goal achieved with {len(actions)} learned actions.{RESET}")
                    print(json.dumps(result, indent=2))
                    return

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print(f"{YELLOW}\nStopped.{RESET}")
    except Exception as error:
        print(f"{RED}\nError: {redact_text(str(error))}{RESET}")
        sys.exit(1)