// Mastra agent + AXPilot over stdio.
//
//   npm install @mastra/core @mastra/mcp
//   npx tsx mastra_example.ts
import { Agent } from "@mastra/core/agent";
import { MCPClient } from "@mastra/mcp";

const mcp = new MCPClient({
  servers: {
    axpilot: { command: "axpilot", args: ["stdio"] },
  },
});

const agent = new Agent({
  name: "Mac operator",
  instructions: "You operate the user's Mac with the AXPilot tools. Observe, act, verify.",
  model: "<your-model>",
  tools: await mcp.getTools(),
});

const result = await agent.generate("List the running apps and the frontmost window's title.");
console.log(result.text);
await mcp.disconnect();
