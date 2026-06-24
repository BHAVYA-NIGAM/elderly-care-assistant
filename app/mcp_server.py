import os
import json
import sys
from datetime import datetime
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("ElderlyCareServer")
DB_FILE = os.path.join(os.path.dirname(__file__), "elderly_care_db.json")

def load_db():
    if not os.path.exists(DB_FILE):
        return {"routines": [], "health_logs": []}
    try:
        with open(DB_FILE, "r") as f:
            return json.load(f)
    except Exception as e:
        print(f"Error loading database: {e}", file=sys.stderr)
        return {"routines": [], "health_logs": []}

def save_db(data):
    try:
        with open(DB_FILE, "w") as f:
            json.dump(data, f, indent=2)
    except Exception as e:
        print(f"Error saving database: {e}", file=sys.stderr)

@mcp.tool()
def get_routines(date: str) -> str:
    """Get the list of daily routines and medication/meditation schedules for a specific date (Format: YYYY-MM-DD)."""
    db = load_db()
    routines = [r for r in db.get("routines", []) if r.get("date") == date]
    if not routines:
        return f"No routines found for {date}."
    return json.dumps(routines, indent=2)

@mcp.tool()
def add_routine(date: str, time: str, activity: str) -> str:
    """Add a new routine, meditation event, medication reminder or appointment to the schedule."""
    db = load_db()
    new_item = {"date": date, "time": time, "activity": activity}
    db.setdefault("routines", []).append(new_item)
    save_db(db)
    return f"Successfully added routine: {activity} at {time} on {date}."

@mcp.tool()
def get_health_logs() -> str:
    """Get the recent health and well-being logs for the elderly patient."""
    db = load_db()
    logs = db.get("health_logs", [])
    if not logs:
        return "No health logs found."
    return json.dumps(logs[-5:], indent=2)  # Return last 5 logs

@mcp.tool()
def add_health_log(symptoms: str, mood: str, blood_pressure: str) -> str:
    """Log the patient's well-being metrics including physical symptoms, mood, and blood pressure."""
    db = load_db()
    new_log = {
        "timestamp": datetime.now().isoformat(),
        "symptoms": symptoms,
        "mood": mood,
        "blood_pressure": blood_pressure
    }
    db.setdefault("health_logs", []).append(new_log)
    save_db(db)
    return f"Successfully logged health status: Symptoms: '{symptoms}', Mood: '{mood}', BP: '{blood_pressure}'."

if __name__ == "__main__":
    mcp.run()
