"""Qualification logic for the Aangan Studio phone agent (build step 2).

Pipeline: transcript -> extract.py (Gemini Flash pulls facts + verbatim quotes)
          -> rules.py (deterministic: 5 gates -> routing -> reason code -> priority -> score).
"""
