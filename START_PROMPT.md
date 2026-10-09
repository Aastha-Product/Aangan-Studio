# Paste this into Claude Code to start

Read CLAUDE.md and every file in context/, data/ and docs/. We're building the Aangan Studio phone enquiry agent described in CLAUDE.md.

Do step 1–3 of the build order only:
1. Write the agent's system prompt (prompts/system_prompt.md) from context/services.md, context/qualified.md and the pricing deflection line. The agent must never say a price.
2. Write the qualification logic: extract fields from a transcript → apply the five gates → route (Qualified P1/P2, Nurture, Not qualified, Escalate) → reason code → interest score out of 100 with a transcript quote behind every point.
3. Run it on the 20 phone transcripts (T01–T20 in data/enquiries.txt) and compare with data/test_set_phone_calls.csv. Show me a table of matches and mismatches, and explain each mismatch.

Don't set up Vaani, Calendly, email, HubSpot or the database yet. Don't change any rule in CLAUDE.md without asking me. Put any API keys you need in .env (copy .env.example), never in code.
