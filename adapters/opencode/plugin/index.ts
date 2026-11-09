// Copyright 2026 Google LLC.
// SPDX-License-Identifier: Apache-2.0

// Server plugin for the experimental OpenCode V2 adapter (#282). The installer passes
// { primary: "egm", bundle: "<bundle root>", reviewers: { "<agent ID>": ["<required absolute path>", ...] } }.

type Part = { type: string; text?: string; metadata?: any }

// Reviewers keep only these host instruction sources. core/instructions carries every
// AGENTS.md and configured instruction file; Code Mode, MCP and skill guidance are parent context.
const KEEP = new Set(["core/date", "core/environment"])
const REVIEWER_TOOLS = new Set(["read", "glob"])

const normalize = (path: unknown) => (typeof path === "string" ? path.replace(/\/+/g, "/") : "")

// Mirrors the installer's ordered read rules: deny *.env and *.env.*, then allow *.env.example.
const secret = (resource: string) => /\.env($|\.)/.test(resource) && !/\.env\.example$/.test(resource)

const completedReads = (messages: any[]) =>
  new Set(
    messages
      .filter((message) => message.type === "assistant")
      .flatMap((message) => message.content ?? [])
      .filter(
        (part) =>
          part.type === "tool" &&
          part.name === "read" &&
          part.state?.status === "completed" &&
          JSON.stringify(part.state.content ?? "").trim().length > 2,
      )
      .map((part) => normalize(part.state.input?.path)),
  )

export default {
  id: "egm-guard",
  async setup(ctx: any) {
    const primary: string = ctx.options?.primary ?? "egm"
    const bundle: string = ctx.options?.bundle
    const reviewers: Record<string, string[]> = ctx.options?.reviewers ?? {}
    const isReviewer = (agent: unknown): agent is string => typeof agent === "string" && Object.hasOwn(reviewers, agent)

    const isolate = async (event: any) => {
      if (!isReviewer(event.agent)) return
      event.system = (event.system ?? []).flatMap((part: Part) => {
        const sources = part.metadata?.experimental?.instructions
        if (Array.isArray(sources)) {
          const kept = sources.filter((source: any) => KEEP.has(source.key)).map((source: any) => source.text)
          return kept.length ? [{ ...part, text: kept.join("\n\n") }] : []
        }
        // Without source metadata an instruction block cannot be told apart from host text.
        return part.text?.includes("Instructions from: ") ? [] : [part]
      })
      // Nested AGENTS.md files arrive as synthetic messages after a read below them.
      const history = await ctx.session.context({ sessionID: event.sessionID })
      const injected = new Set(
        history.filter((message: any) => message.type === "synthetic" && message.metadata?.instruction).map((message: any) => message.id),
      )
      event.messages = (event.messages ?? []).filter((message: any) => !injected.has(message.id))
      for (const tool of Object.keys(event.tools ?? {})) if (!REVIEWER_TOOLS.has(tool)) delete event.tools[tool]
    }
    await ctx.session.hook("context", isolate)
    await ctx.session.hook("compaction", isolate)

    // Every review is a fresh foreground child: never resume a reviewer session.
    await ctx.tool.hook("execute.before", (event: any) => {
      if (event.tool !== "subagent" || event.agent !== primary || !isReviewer(event.input?.agent)) return
      const { sessionID: _session, background: _background, ...input } = event.input
      event.input = input
    })

    await ctx.tool.hook("execute.after", async (event: any) => {
      if (event.tool !== "subagent" || event.agent !== primary || event.status !== "completed") return
      const agent = event.input?.agent
      if (!isReviewer(agent)) return
      const child = event.result?.metadata?.sessionID
      const reads = child ? completedReads(await ctx.session.context({ sessionID: child })) : new Set<string>()
      const missing = reviewers[agent].filter((path) => !reads.has(normalize(path)))
      if (!missing.length) return
      const notice =
        `EGM: incomplete review. ${agent} did not complete required reads: ${missing.join(", ")}. ` +
        "Do not use this review; report it as incomplete."
      event.result = {
        ...event.result,
        content: [{ type: "text", text: notice }],
        metadata: { ...event.result?.metadata, egmMissingReads: missing },
      }
    })

    // Project config and session rules can append allows after the installer's reviewer rules;
    // configured denies never reach this hook, so it only tightens.
    await ctx.permission.hook("evaluate", (event: any) => {
      const resources: string[] = event.resources ?? []
      if (isReviewer(event.agent)) {
        const permitted =
          event.action === "glob" ||
          (event.action === "read" && !resources.some(secret)) ||
          (event.action === "external_directory" && resources.every((resource) => resource.startsWith(`${bundle}/`)))
        if (!permitted) {
          event.effect = "deny"
          event.message = "EGM reviewers may only read the project and the EGM bundle."
        }
        return
      }
      // The installer sets reviewer dispatch to "ask", so a missing plugin surfaces as a prompt.
      if (event.action === "subagent" && event.agent === primary && event.effect === "ask" && resources.length && resources.every(isReviewer)) {
        event.effect = "allow"
      }
    })
  },
}
