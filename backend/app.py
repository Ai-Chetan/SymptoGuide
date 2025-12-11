from flask import Flask, request, jsonify
from flask_cors import CORS
import os
import requests
from math import radians, cos, sin, asin, sqrt

print("Starting Flask backend...")

app = Flask(__name__)
CORS(app)

GEOAPIFY_KEY = os.environ.get("GEOAPIFY_KEY")


def haversine_distance_km(lat1, lon1, lat2, lon2):
    """
    Compute great-circle distance between two points (lat/lon) in kilometers.
    """
    try:
        lat1, lon1, lat2, lon2 = map(radians, [lat1, lon1, lat2, lon2])
        dlon = lon2 - lon1
        dlat = lat2 - lat1
        a = sin(dlat / 2) ** 2 + cos(lat1) * cos(lat2) * sin(dlon / 2) ** 2
        c = 2 * asin(sqrt(a))
        R = 6371  # Earth radius in km
        return R * c
    except Exception:
        return None


@app.route("/", methods=["GET"])
def health():
    return jsonify({"status": "ok", "service": "backend"}), 200


@app.route("/api/assess", methods=["POST"])
def assess():
    data = request.get_json(silent=True) or {}

    symptoms = data.get("symptoms", [])
    severity = data.get("severity", "")
    duration = data.get("duration", "")

    concern = "low"

    red_flag_symptoms = {
        "chest pain",
        "difficulty breathing",
        "shortness of breath",
        "loss of consciousness",
        "severe bleeding",
        "sudden weakness",
    }

    lower_symptoms = {s.lower() for s in symptoms}

    if lower_symptoms & red_flag_symptoms:
        concern = "high"
    elif severity == "severe":
        concern = "high"
    elif severity == "moderate" or duration in {"week", "weeks", "month", "chronic"}:
        concern = "moderate"
    elif len(symptoms) >= 3:
        concern = "moderate"

    dept = "Primary Care"

    if any(
        k in lower_symptoms
        for k in ["stomach pain", "abdominal pain", "nausea", "vomiting", "diarrhea", "bloating"]
    ):
        dept = "Gastroenterology"
    elif any(k in lower_symptoms for k in ["chest pain", "palpitations", "heart"]):
        dept = "Cardiology"
    elif any(
        k in lower_symptoms
        for k in ["shortness of breath", "breathlessness", "cough", "wheezing"]
    ):
        dept = "Pulmonology"
    elif any(k in lower_symptoms for k in ["headache", "migraine", "dizziness", "numbness", "seizure"]):
        dept = "Neurology"
    elif any(k in lower_symptoms for k in ["rash", "itching", "skin", "hives"]):
        dept = "Dermatology"

    recommended_departments = []

    if concern == "high":
        recommended_departments.append("Emergency")

    if dept != "Primary Care":
        recommended_departments.append(dept)

    if not recommended_departments:
        if concern == "moderate":
            recommended_departments = ["General Medicine"]
        else:
            recommended_departments = ["Primary Care"]

    return jsonify(
        {
            "concern_level": concern,
            "suggestions": [
                "This is a preliminary assessment and not a diagnosis.",
                "If concern is high, seek emergency care.",
            ],
            "recommended_departments": recommended_departments,
        }
    )


# ----------------- Geoapify-based nearby hospitals -----------------
@app.route("/api/nearby-hospitals", methods=["GET"])
def nearby_hospitals():
    """
    Query Geoapify for healthcare places around (lat, lng) within radius_m.
    Accepts optional ?department=Orthopedics (case-insensitive).
    Returns hospitals sorted by distance and pre-filtered by department when possible.
    """
    try:
        lat = float(request.args.get("lat"))
        lng = float(request.args.get("lng"))
    except (TypeError, ValueError):
        return jsonify({"error": "lat and lng query params required"}), 400

    department_raw = (request.args.get("department") or "").strip()
    department = department_raw.lower()

    # radius in meters (default 20 km)
    radius_m = int(request.args.get("radius_m") or 20000)

    if not GEOAPIFY_KEY:
        print("Geoapify key missing or empty")
        return jsonify({"error": "Geoapify API key not configured"}), 500

    # Map friendly department name -> Geoapify category
    # Use only safe, supported categories:
    #   healthcare, healthcare.hospital, healthcare.dentist [web:211]
    dept_map = {
        # for all specialties, just search general healthcare places
        "cardiology": "healthcare",
        "neurology": "healthcare",
        "dermatology": "healthcare",
        "orthopedics": "healthcare",
        "gastroenterology": "healthcare",
        "pulmonology": "healthcare",
        "ent": "healthcare",
        # specific categories that are supported
        "dental": "healthcare.dentist",
        "dentistry": "healthcare.dentist",
        "emergency": "healthcare.hospital",
        "general medicine": "healthcare",
        "primary care": "healthcare",
    }

    # pick a category param if we have a mapping; otherwise use broad healthcare query
    geo_category = "healthcare"
    dept_key = department.strip().lower()
    if dept_key in dept_map:
        geo_category = dept_map[dept_key]

    url = "https://api.geoapify.com/v2/places"
    params = {
        "categories": geo_category,
        "filter": f"circle:{lng},{lat},{radius_m}",
        "limit": 100,
        "apiKey": GEOAPIFY_KEY,
    }

    try:
        resp = requests.get(url, params=params, timeout=8)
        print("Geoapify status:", resp.status_code)
        print("Geoapify body preview:", resp.text[:300])
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        print("Geoapify error:", e)
        return jsonify(
            {
                "error": "Failed to fetch hospitals from Geoapify",
                "details": str(e),
            }
        ), 500

    features = data.get("features", [])

    hospitals_all = []
    for i, feat in enumerate(features):
        props = feat.get("properties", {}) or {}
        geom = feat.get("geometry", {}) or {}
        coords = geom.get("coordinates") or [None, None]
        lng_res, lat_res = (coords[0], coords[1]) if len(coords) >= 2 else (None, None)

        name = props.get("name") or ""
        if not name:
            continue

        address = (
            props.get("formatted")
            or props.get("address_line1")
            or "Address not available"
        )
        categories = props.get("categories") or []
        cats_lower = [c.lower() for c in categories]
        name_lower = name.lower()
        inferred_specialties = []

        # Infer specialties using name + categories (loose matching)
        if any("cardio" in c or "heart" in c for c in cats_lower) or "cardio" in name_lower or "heart" in name_lower:
            inferred_specialties.append("Cardiology")
        if any("gastro" in c for c in cats_lower) or "gastro" in name_lower:
            inferred_specialties.append("Gastroenterology")
        if any("neuro" in c for c in cats_lower) or "neuro" in name_lower:
            inferred_specialties.append("Neurology")
        if any("dermatology" in c for c in cats_lower) or "derma" in name_lower or "skin" in name_lower:
            inferred_specialties.append("Dermatology")
        if "ent" in name_lower or "ear nose throat" in name_lower:
            inferred_specialties.append("ENT")
        if any("ortho" in c for c in cats_lower) or "ortho" in name_lower or "bone" in name_lower:
            inferred_specialties.append("Orthopedics")
        if "dent" in name_lower or "dental" in name_lower or "dentist" in name_lower:
            inferred_specialties.append("Dental")
        if "surgical" in name_lower:
            inferred_specialties.append("Surgery")

        emergency = (
            "emergency" in name_lower
            or "er" in name_lower
            or "24/7" in (props.get("opening_hours") or "")
        )

        distance_km = None
        if lat_res is not None and lng_res is not None:
            try:
                distance_km = haversine_distance_km(
                    lat, lng, float(lat_res), float(lng_res)
                )
            except Exception:
                distance_km = None

        hospitals_all.append(
            {
                "id": str(props.get("place_id", i)),
                "name": name,
                "address": address,
                "lat": lat_res,
                "lng": lng_res,
                "emergency": bool(emergency),
                "phone": props.get("contact:phone") or "Not available",
                "specialties": inferred_specialties,
                "has_specialties": bool(inferred_specialties),
                "rating": props.get("rate", 4.5) or 4.5,
                "distance_km": distance_km if distance_km is not None else None,
            }
        )

    # Normalize: sort by distance (unknown distances come last)
    hospitals_all.sort(
        key=lambda h: (
            h["distance_km"] is None,
            h["distance_km"] if h["distance_km"] is not None else 99999,
        )
    )

    # Filter by requested department (server-side)
    def matches_department(h, dept):
        if not dept:
            return True
        dept_norm = dept.lower()
        if dept_norm == "emergency":
            return h["emergency"] is True or any(
                "emergency" in s.lower() for s in (h.get("specialties") or [])
            )
        specialties_lower = [(s or "").lower() for s in (h.get("specialties") or [])]
        if any(dept_norm in s for s in specialties_lower):
            return True
        synonyms = {
            "dental": ["dental", "dentist"],
            "orthopedics": ["ortho", "orthopedics", "bone"],
            "cardiology": ["cardio", "cardiology", "heart"],
            "neurology": ["neuro", "neurology"],
            "dermatology": ["derma", "dermatology", "skin"],
            "gastroenterology": ["gastro", "gastroenterology"],
            "ent": ["ent", "ear", "nose", "throat"],
            "primary care": ["general", "primary", "family"],
            "general medicine": ["general", "medicine", "gp", "general medicine"],
        }
        for key, vals in synonyms.items():
            if dept_norm == key:
                name_addr_lower = (
                    (h.get("name", "") or "") + " " + (h.get("address", "") or "")
                ).lower()
                if any(v in name_addr_lower for v in vals):
                    return True
        return False

    filtered = [h for h in hospitals_all if matches_department(h, department)]

    # Fallback: if nothing matched and a department was requested, return nearest general hospitals
    fallback_used = False
    if department and len(filtered) == 0:
        fallback_used = True
        filtered = hospitals_all[:15]

    def format_distance(h):
        dk = h.get("distance_km")
        if dk is None:
            return ""
        if dk < 1:
            return f"{int(dk * 1000)} m"
        else:
            return f"{dk:.1f} km"

    results = []
    for h in filtered:
        results.append(
            {
                "id": h["id"],
                "name": h["name"],
                "address": h["address"],
                "lat": h["lat"],
                "lng": h["lng"],
                "emergency": h["emergency"],
                "phone": h["phone"],
                "specialties": h["specialties"],
                "has_specialties": h["has_specialties"],
                "rating": round(
                    float(h.get("rating", 4.5)) if h.get("rating") else 4.5, 1
                ),
                "distance": format_distance(h),
            }
        )

    return jsonify({"hospitals": results, "fallback_used": fallback_used})


if __name__ == "__main__":
    print("Starting Flask development server on http://127.0.0.1:5000")
    app.run(debug=True, port=5000)
