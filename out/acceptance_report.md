# Acceptance test: T01–T20

**20/20 match** on decision + priority + reason code.  
Gemini tokens: 31,496 in · 11,509 out · 30,413 thinking

| Call | Expected | Got | Score | Match | Flags (got) | Flags (expected) |
|---|---|---|---|---|---|---|
| T01 | Qualified P2 | Qualified P2 | 100 | ✅ | Referral (Shruti Joshi) | Referral (Shruti Joshi) |
| T02 | Qualified P1 | Qualified P1 | 95 | ✅ | Asked for price x2. Deflected, NOT quoted | Asked for price - deflected and booked |
| T03 | Not qualified OUT_OF_AREA | Not qualified OUT_OF_AREA | 30 | ✅ | Gate 3 Realistic timeline: timeline not asked |  |
| T04 | Not qualified ADVICE_ONLY | Not qualified ADVICE_ONLY | 10 | ✅ | Wants site visit<br>Gate 2 Service area: location not established<br>Gate 3 Realistic timeline: timeline not asked<br>Gate 5 Decision-maker: decision-maker not confirmed (treated as qualified) |  |
| T05 | Qualified P1 | Qualified P1 | 100 | ✅ | Referral (Vikram Agarwal)<br>Wants site visit | Referral (Vikram Agarwal); wants site visit |
| T06 | Qualified P1 | Qualified P1 | 60 | ✅ | Commercial | Commercial |
| T07 | Nurture TIMELINE_IMPOSSIBLE | Nurture TIMELINE_IMPOSSIBLE | 60 | ✅ | Called outside 10am–7pm<br>Gate 2 Service area: location not established | Open to a November start |
| T08 | No data NO_INFO | No data NO_INFO | 0 | ✅ | No conversation. The agent would have answered this call | Agent would have answered |
| T09 | Escalate EXISTING_CLIENT_COMPLAINT | Escalate EXISTING_CLIENT_COMPLAINT | 0 | ✅ | Existing client. Instant alert to studio head; senior callback promised | Senior callback within 15 min |
| T10 | Not qualified BUDGET_MISALIGNED | Not qualified BUDGET_MISALIGNED | 35 | ✅ | Gate 2 Service area: Kharadi: not on the services.md list. Confirm area<br>Gate 3 Realistic timeline: timeline not asked |  |
| T11 | Qualified P2 | Qualified P2 | 55 | ✅ | Rented. Reversible fittings only<br>Gate 3 Realistic timeline: timeline not asked | Rented - reversible fittings; timeline not asked |
| T12 | Qualified P1 | Qualified P1 | 65 | ✅ | Wants site visit | Wants principal designer on site |
| T13 | Qualified P2 | Qualified P2 | 65 | ✅ | Asked for price x2. Deflected, NOT quoted<br>Gate 3 Realistic timeline: timeline not asked | Asked for price - deflected and booked; timeline not asked |
| T14 | Qualified P2 | Qualified P2 | 38 | ✅ | Gate 3 Realistic timeline: timeline not asked<br>Gate 5 Decision-maker: decision with others. They must attend the consultation | Decision-maker unclear - parents must attend |
| T15 | Qualified P1 | Qualified P1 | 90 | ✅ | Wants site visit | Builder allows site visit now |
| T16 | Qualified P1 | Qualified P1 | 0 | ✅ | Returning caller we let down. Handle with care<br>Gate 3 Realistic timeline: timeline not asked<br>Gate 5 Decision-maker: decision-maker not confirmed (treated as qualified) | Frustrated returning caller - handle with care |
| T17 | Qualified P2 | Qualified P2 | 60 | ✅ | Line dropped once and called back | Line dropped once and called back |
| T18 | Not qualified TOO_SMALL | Not qualified TOO_SMALL | 35 | ✅ | Commercial<br>Gate 2 Service area: location not established<br>Gate 3 Realistic timeline: timeline not asked | Minimum office size not in services.md |
| T19 | Not qualified OUT_OF_SCOPE_TYPE | Not qualified OUT_OF_SCOPE_TYPE | 15 | ✅ | Gate 3 Realistic timeline: timeline not asked |  |
| T20 | Qualified P2 | Qualified P2 | 90 | ✅ | Called outside 10am–7pm | Both owners will attend |

## Per-call detail

### T01: Qualified P2
- Gate 1 Real project, in scope: ✅
- Gate 2 Service area: ✅  Kothrud — Dahanukar Colony
- Gate 3 Realistic timeline: ✅  Done by March, no rush
- Gate 4 Budget band (never asked): ✅  not mentioned
- Gate 5 Decision-maker: ✅
- **Interest 100/100**
  - Commitment 30/30 "Sure. (gives number)"
  - Readiness 25/25 "We’d like to be done by March."
  - Scope clarity 20/20 "kitchen, living room, both bedrooms … About 1,400 sq ft carpet."
  - Decision-maker 15/15 "Yes — myself and my husband. He knows we’re calling and is happy to go ahead."
  - Warm source 10/10 "I got your number from a friend — Shruti Joshi."

### T02: Qualified P1
Priority because: fixed date: move in  
- Gate 1 Real project, in scope: ✅
- Gate 2 Service area: ✅  Wakad
- Gate 3 Realistic timeline: ✅  Move in November, design starting from October
- Gate 4 Budget band (never asked): ✅  not mentioned
- Gate 5 Decision-maker: ✅
- **Interest 95/100**
  - Commitment 30/30 "Okay, fair enough. Let’s book it."
  - Readiness 25/25 "We move in November — so maybe starting design from October?"
  - Scope clarity 20/20 "modular kitchen, wardrobes, living room, both bedrooms … About 950 sq ft"
  - Decision-maker 15/15 "I have a 2BHK in Wakad"
  - Warm source 5/10 "I was looking at your work on Instagram."

### T03: Not qualified  OUT_OF_AREA
- Gate 1 Real project, in scope: ✅
- Gate 2 Service area: ❌ OUT_OF_AREA Nashik is outside Pune/PCMC
- Gate 3 Realistic timeline: ❔  timeline not asked
- Gate 4 Budget band (never asked): ✅  not mentioned
- Gate 5 Decision-maker: ✅
- **Interest 30/100**
  - Commitment 0/30
  - Readiness 0/25
  - Scope clarity 10/20 "I want to redo my home office and study."
  - Decision-maker 15/15 "I want to redo my home office and study."
  - Warm source 5/10 "I saw your work on LinkedIn."

### T04: Not qualified  ADVICE_ONLY
- Gate 1 Real project, in scope: ❌ ADVICE_ONLY wants advice or ideas only
- Gate 2 Service area: ❔  location not established
- Gate 3 Realistic timeline: ❔  timeline not asked
- Gate 4 Budget band (never asked): ✅  not mentioned
- Gate 5 Decision-maker: ❔  decision-maker not confirmed (treated as qualified)
- **Interest 10/100**
  - Commitment 0/30
  - Readiness 0/25
  - Scope clarity 10/20 "ideas for our living room"
  - Decision-maker 0/15
  - Warm source 0/10

### T05: Qualified P1
Priority because: fixed date: move in  
- Gate 1 Real project, in scope: ✅
- Gate 2 Service area: ✅  Koregaon Park
- Gate 3 Realistic timeline: ✅  Move back in by February, about 4 months
- Gate 4 Budget band (never asked): ✅  not mentioned
- Gate 5 Decision-maker: ✅
- **Interest 100/100**
  - Commitment 30/30 "Yes please — Vikram said your team does that."
  - Readiness 25/25 "We want to move back in by February. About 4 months."
  - Scope clarity 20/20 "New flooring, kitchen, all four bedrooms. … about 2,400 sq ft carpet"
  - Decision-maker 15/15 "We have a 4BHK in Koregaon Park"
  - Warm source 10/10 "Vikram Agarwal asked me to call — he’s a close friend of Nikhil’s."

### T06: Qualified P1
Priority because: fixed date: go live  
- Gate 1 Real project, in scope: ✅
- Gate 2 Service area: ✅  Baner
- Gate 3 Realistic timeline: ✅  operational by December
- Gate 4 Budget band (never asked): ✅  not mentioned
- Gate 5 Decision-maker: ✅
- **Interest 60/100**
  - Commitment 0/30
  - Readiness 25/25 "We want to be operational by December."
  - Scope clarity 20/20 "workstations for 20 people, a small cabin, a meeting room, and a break area. … About 800 sq ft."
  - Decision-maker 15/15 "Yes, I’m the founder."
  - Warm source 0/10

### T07: Nurture  TIMELINE_IMPOSSIBLE
- Gate 1 Real project, in scope: ✅
- Gate 2 Service area: ❔  location not established
- Gate 3 Realistic timeline: ❌ TIMELINE_IMPOSSIBLE needed in ~3 weeks (minimum 6)
- Gate 4 Budget band (never asked): ✅  not mentioned
- Gate 5 Decision-maker: ✅
- **Interest 60/100**
  - Commitment 10/30 "Let me think and call back."
  - Readiness 25/25 "before Diwali"
  - Scope clarity 10/20 "living room and kitchen"
  - Decision-maker 15/15 "I want to redo my living room and kitchen before Diwali."
  - Warm source 0/10

### T08: No data  NO_INFO

### T09: Escalate  EXISTING_CLIENT_COMPLAINT

### T10: Not qualified  BUDGET_MISALIGNED
- Gate 1 Real project, in scope: ✅
- Gate 2 Service area: ✅  Kharadi: not on the services.md list. Confirm area
- Gate 3 Realistic timeline: ❔  timeline not asked
- Gate 4 Budget band (never asked): ❌ BUDGET_MISALIGNED volunteered budget clearly below scope
- Gate 5 Decision-maker: ✅
- **Interest 35/100**
  - Commitment 0/30
  - Readiness 0/25
  - Scope clarity 20/20 "I want to do the kitchen and one bedroom. … about 550 sq ft"
  - Decision-maker 15/15 "I have a 1BHK in Kharadi"
  - Warm source 0/10

### T11: Qualified P2
- Gate 1 Real project, in scope: ✅
- Gate 2 Service area: ✅  Baner
- Gate 3 Realistic timeline: ❔  timeline not asked
- Gate 4 Budget band (never asked): ✅  not mentioned
- Gate 5 Decision-maker: ✅
- **Interest 55/100**
  - Commitment 30/30 "Yes please."
  - Readiness 0/25
  - Scope clarity 10/20 "living room, bedroom, and kitchen"
  - Decision-maker 15/15 "Yes — they’re fine with it as long as we don’t break walls."
  - Warm source 0/10

### T12: Qualified P1
Priority because: fixed date: move in, wants to meet within 2 weeks  
- Gate 1 Real project, in scope: ✅
- Gate 2 Service area: ✅  Kalyani Nagar
- Gate 3 Realistic timeline: ✅  March next year to move in
- Gate 4 Budget band (never asked): ✅  not mentioned
- Gate 5 Decision-maker: ✅
- **Interest 65/100**
  - Commitment 30/30 "Yes — this week or next if possible."
  - Readiness 25/25 "March next year to move in."
  - Scope clarity 10/20 "about 5,500 sq ft"
  - Decision-maker 0/15 quote not found in transcript, 0 points
  - Warm source 0/10

### T13: Qualified P2
- Gate 1 Real project, in scope: ✅
- Gate 2 Service area: ✅  Aundh
- Gate 3 Realistic timeline: ❔  timeline not asked
- Gate 4 Budget band (never asked): ✅  not mentioned
- Gate 5 Decision-maker: ✅
- **Interest 65/100**
  - Commitment 30/30 "Fine. Let’s book then."
  - Readiness 0/25
  - Scope clarity 20/20 "Kitchen, wardrobes in both bedrooms, living room. … about 1,100 sq ft"
  - Decision-maker 15/15 "I want to redo my flat in Aundh."
  - Warm source 0/10

### T14: Qualified P2
- Gate 1 Real project, in scope: ✅
- Gate 2 Service area: ✅  Hadapsar
- Gate 3 Realistic timeline: ❔  timeline not asked
- Gate 4 Budget band (never asked): ✅  not mentioned
- Gate 5 Decision-maker: ✅  decision with others. They must attend the consultation
- **Interest 38/100**
  - Commitment 30/30 "Please. (gives number)"
  - Readiness 0/25
  - Scope clarity 0/20
  - Decision-maker 8/15 "Yes, they would come."
  - Warm source 0/10

### T15: Qualified P1
Priority because: fixed date: possession  
- Gate 1 Real project, in scope: ✅
- Gate 2 Service area: ✅  Undri
- Gate 3 Realistic timeline: ✅  possession in about six weeks, wanting to start design right away
- Gate 4 Budget band (never asked): ✅  not mentioned
- Gate 5 Decision-maker: ✅
- **Interest 90/100**
  - Commitment 30/30 "He said to go ahead and book the consultation."
  - Readiness 25/25 "possession of my flat in Undri in about six weeks."
  - Scope clarity 20/20 "Full home — kitchen, wardrobes, living room. … 875 sq ft."
  - Decision-maker 15/15 "Yes — my husband and I. He said to go ahead and book the consultation."
  - Warm source 0/10

### T16: Qualified P1
Priority because: returning caller we let down  
- Gate 1 Real project, in scope: ✅  design + execution assumed, not confirmed
- Gate 2 Service area: ✅  Viman Nagar
- Gate 3 Realistic timeline: ❔  timeline not asked
- Gate 4 Budget band (never asked): ✅  not mentioned
- Gate 5 Decision-maker: ❔  decision-maker not confirmed (treated as qualified)
- **Interest 0/100**
  - Commitment 0/30
  - Readiness 0/25
  - Scope clarity 0/20
  - Decision-maker 0/15
  - Warm source 0/10

### T17: Qualified P2
- Gate 1 Real project, in scope: ✅
- Gate 2 Service area: ✅  Pimple Saudagar
- Gate 3 Realistic timeline: ✅  Ideally done by March
- Gate 4 Budget band (never asked): ✅  not mentioned
- Gate 5 Decision-maker: ✅
- **Interest 60/100**
  - Commitment 0/30
  - Readiness 25/25 "Ideally done by March. Plenty of time."
  - Scope clarity 20/20 "Kitchen, wardrobes, living room. … about 1,050 sq ft"
  - Decision-maker 15/15 "We’ve been here two years"
  - Warm source 0/10

### T18: Not qualified  TOO_SMALL
- Gate 1 Real project, in scope: ❌ TOO_SMALL 180 sq ft is below the provisional 500 sq ft office minimum (pending Nikhil)
- Gate 2 Service area: ❔  location not established
- Gate 3 Realistic timeline: ❔  timeline not asked
- Gate 4 Budget band (never asked): ✅  not mentioned
- Gate 5 Decision-maker: ✅
- **Interest 35/100**
  - Commitment 0/30
  - Readiness 0/25
  - Scope clarity 20/20 "I have a 180 sq ft pod I want to make look really nice. … 180 sq ft pod"
  - Decision-maker 15/15 "I run a small coworking space."
  - Warm source 0/10

### T19: Not qualified  OUT_OF_SCOPE_TYPE
- Gate 1 Real project, in scope: ❌ OUT_OF_SCOPE_TYPE restaurant hotel hospitality is out of scope
- Gate 2 Service area: ✅  Koregaon Park
- Gate 3 Realistic timeline: ❔  timeline not asked
- Gate 4 Budget band (never asked): ✅  not mentioned
- Gate 5 Decision-maker: ✅
- **Interest 15/100**
  - Commitment 0/30
  - Readiness 0/25
  - Scope clarity 0/20
  - Decision-maker 15/15 "I’m planning to open a restaurant in Koregaon Park."
  - Warm source 0/10

### T20: Qualified P2
- Gate 1 Real project, in scope: ✅
- Gate 2 Service area: ✅  Magarpatta — Cybercity area
- Gate 3 Realistic timeline: ✅  January start for execution
- Gate 4 Budget band (never asked): ✅  not mentioned
- Gate 5 Decision-maker: ✅
- **Interest 90/100**
  - Commitment 30/30 "Please."
  - Readiness 25/25 "a January start for execution would be ideal."
  - Scope clarity 20/20 "Kitchen, both bedrooms, living room. … About 900 sq ft."
  - Decision-maker 15/15 "Yes — my husband and I. We both want to be at the consultation."
  - Warm source 0/10
