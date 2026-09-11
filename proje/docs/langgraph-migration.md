# LangGraph migration notes

Working copy only: `agent-webapp-langgraph` (ports **8081** / **8001**).
Camel production (`agent-webapp`, **8080** / **8000**) must stay untouched.

## Executor switch

| Env | Behavior |
|-----|----------|
| `AGENT_EXECUTOR=camel` (default) | Existing Camel Single + Workforce paths |
| `AGENT_EXECUTOR=langgraph` | LangGraph Single/Direct **and** Workforce supervisor |

## Smoke checklist (both stacks)

Camel stack (`8080` / `8000`):

1. UI loads at http://127.0.0.1:8080
2. Backend docs at http://127.0.0.1:8000/docs
3. Single-mode short prompt returns streamed answer
4. Workforce-mode prompt shows workers / tasks in UI

LangGraph copy (`8081` / **8001**):

1. UI loads at http://127.0.0.1:8081
2. Backend docs at http://127.0.0.1:8001/docs
3. With `AGENT_EXECUTOR=camel`: same as above (regression)
4. With `AGENT_EXECUTOR=langgraph`: Single-mode streams; Workforce shows Research/Document workers via LangGraph

## Phases

0. Baseline + port isolation  
1. Adapter + flag (Camel default)  
2. Single Agent LangGraph  
3. Helper LLM calls off Camel  
4. Workforce supervisor on LangGraph  
5. Remove Camel deps after proof  
