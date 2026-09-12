# AiRTraffic Control — UI design notes

Quiet dark ops console (Apple Settings / visionOS vibrancy + dashboard hierarchy research).

## Sources
- Apple HIG Materials / Liquid Glass: content on surface ladder; vibrancy for label / secondary / tertiary; prefer translucent separation over heavy chrome
- SaaS dashboard practice: F-pattern, top-left KPIs, progressive disclosure, color reserved for state
- Linear-style restraint: near-black canvas (not pure #000), hairlines over shadows, one accent

## Hierarchy
1. Fleet KPIs (running / paused / killed / channel)
2. Voice primary action
3. Dense worker table
4. Audit + text command as secondary

## Tokens
- Canvas `#0b0b0d`, surface `#121214`, hairline `rgba(255,255,255,0.08)`
- Accent Apple system blue `#0a84ff`
- Status only: green / amber / red
