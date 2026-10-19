// Vercel AI SDK + AXPilot over stdio.
//
//   npm install ai @ai-sdk/mcp
//   npx tsx ai_sdk_example.ts
import { createMCPClient } from "@ai-sdk/mcp";
import { Experimental_StdioMCPTransport as StdioMCPTransport } from "@ai-sdk/mcp/mcp-stdio";
import { generateText, stepCountIs } from "ai";

const axpilot = await createMCPClient({
  transport: new StdioMCPTransport({ command: "axpilot", args: ["stdio"] }),
});

try {
  const { text } = await generateText({
    model: "<your-model>",
    tools: await axpilot.tools(),
    stopWhen: stepCountIs(25),
    system: "You operate the user's Mac with the AXPilot tools. Observe, act, verify.",
    prompt: "Open Notes and read me the newest note.",
  });
  console.log(text);
} finally {
  await axpilot.close();
}
