# jiraku: agent-powered jira triage agent

An automated triage system that proactively monitors Jira for new, unassigned, or untriaged tickets and seamlessly delegages them to specialized agents.

## Workflow

1. **Polling Service:** A scheduled background service will poll Jira at defined intervals (e.g., every 30 minutes) specifically filtering for issues with an "Untriaged" or "To Do" status.  
2. **Intent Classification:** Upon retrieving a batch of new tickets, an LLM-based agent will analyze the ticket description, priority, and reporter to classify the task.  
3. **Agent Handoff:**  
   * Based on the classification (e.g., "Bug," "Feature Request," "Documentation", which project it pertains to), the system will route the ticket to the appropriate functional agent.  
   * The specialized agent will perform initial validation (e.g., checking if the bug is reproducible or if the feature request is a duplicate).  
4. **Status Transition:** Once the specialized agent confirms the ticket is actionable, it will use the Jira API to update the status to "In-Progress," and begin working on the ticket. If the ticket is deemed invalid or requires further clarification, it will be surfaced to the jiraku dashboard for human review.
5. **Exception Handling:** Any tickets that cannot be confidently classified or require human intervention will be surfaced in the jiraku dashboard for manual triage. New rules will be learned over time based on these exceptions, improving the system's accuracy.

## Running jiraku

jiraku can be run using [Canonical Workshop](https://ubuntu.com/workshop).

To get started, [install Workshop](https://ubuntu.com/workshop/docs/tutorial/part-1-get-started/#install-ws-markup) and run the following commands:

```bash
workshop launch
workshop run jiraku
```

jiraku runs using Github Copilot CLI as the main agent. Support for additional CLI agents may be planned for the future.

## Interacting with jiraku

jiraku has a TUI-based dashboard that allows users to monitor the status of tickets, view agent activity, and manage exceptions surfaced by the triage agent or the worker agents. The dashboard will provide real-time insights into the triage process, including metrics such as number of tickets processed and agent performance.

## Implementation details

jiraku is implemented in Python and leverages the following libraries and tools:

- uv for managing the Python environment

The core of jiraku is isolated with a hexagonal architecture, ensuring that the business logic is decoupled from external dependencies. The system is designed to be modular, allowing for easy integration of new agents and classification models as needed, as well as new front-ends for interacting with the user.