// Copyright 2026 Google LLC.
// SPDX-License-Identifier: Apache-2.0

// Experimental OpenCode 2.x spike for #282, not wired into the installer. It keeps inherited
// instructions and the skill tool away from EGM reviewers, and flags a reviewer result whose
// required reads did not complete. Options: { required: { "<reviewer>": ["<absolute path>"] } }.

type Part = { type: string; text?: string }

const isReviewer = (agent: string | undefined) => !!agent && agent.startsWith("egm-")

// OpenCode renders each instruction source as "Instructions from: <source>" plus its content,
// in the same system part as the date and the <env> block, which reviewers still need.
const INSTRUCTION_BLOCK = /Instructions from: [^\n]*\n[\s\S]*?(?:\n\n\n|$)(?=Instructions from: |Today's date: |$)/g

const completedReads = (messages: any[]) =>
  new Set(
    messages
      .filter((message) => message.type === "assistant")
      .flatMap((message) => message.content ?? [])
      .filter((part) => part.type === "tool" && part.name === "read" && part.state?.status === "completed")
      .map((part) => part.state.input?.path),
  )

export default {
  id: "egm-reviewer-guard",
  async setup(ctx: any) {
    const required: Record<string, string[]> = ctx.options?.required ?? {}

    await ctx.session.hook("context", (event: any) => {
      if (!isReviewer(event.agent)) return
      event.system = event.system
        .map((part: Part) =>
          part.type === "text" && part.text ? { ...part, text: part.text.replace(INSTRUCTION_BLOCK, "") } : part,
        )
        .filter((part: Part) => part.type !== "text" || part.text)
      delete event.tools.skill
    })

    await ctx.tool.hook("execute.after", async (event: any) => {
      if (event.tool !== "subagent" || event.status !== "completed") return
      const agent = event.input?.agent
      const paths = required[agent]
      const child = event.result?.metadata?.sessionID
      if (!paths?.length || !child) return
      const reads = completedReads(await ctx.session.context({ sessionID: child }))
      const missing = paths.filter((path) => !reads.has(path))
      if (!missing.length) return
      const notice =
        `EGM: incomplete review. ${agent} did not complete required reads: ${missing.join(", ")}. ` +
        "Treat this review as incomplete and do not use its findings."
      event.result = {
        ...event.result,
        content: [{ type: "text", text: notice }, ...(event.result.content ?? [])],
        metadata: { ...event.result.metadata, egmMissingReads: missing },
      }
    })
  },
}
