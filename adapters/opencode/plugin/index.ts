// Copyright 2026 Google LLC.
// SPDX-License-Identifier: Apache-2.0

// Server plugin for the experimental OpenCode V2 adapter (#282). The installer passes
// { primary: "egm", bundle: "<bundle root>", reviewers: { "<agent ID>": "<persona path>" } }.
// Reviewer dispatch is denied in the generated config; this plugin grants it per dispatch,
// so without the plugin no reviewer can start.

import { readFileSync, realpathSync } from "node:fs"
import { isAbsolute, join, relative, sep } from "node:path"

type Part = { type: string; text?: string; metadata?: any }
type Rule = { action: string; resource: string; effect: string }

// core/instructions carries every AGENTS.md and configured instruction file; Code Mode, MCP
// and skill guidance belong to the parent. Reviewers keep only these sources.
const KEEP = new Set(["core/date", "core/environment"])
const REVIEWER_TOOLS = new Set(["read", "glob"])
const READS_HEADER = "EGM required reads:"

const real = (path: string) => {
  try {
    return realpathSync(path)
  } catch {
    return undefined
  }
}
const within = (path: string, root: string) => path === root || path.startsWith(root + sep)
// Mirrors the installer's ordered read rules: deny *.env and *.env.*, then allow *.env.example.
const secret = (path: string) => /\.env($|\.)/.test(path) && !/\.env\.example$/.test(path)

// OpenCode 2.2.6 starts read output with "Read file <path>, lines A-B" or "Read file <path>, 0 lines".
const readRange = (text: string): [number, number] | undefined => {
  const match = /, lines (\d+)-(\d+)$/.exec(text.split("\n", 1)[0])
  return match ? [Number(match[1]), Number(match[2])] : undefined
}
// Unreadable files count as empty, so they can never satisfy a required read.
const lineCount = (path: string) => {
  let text = ""
  try {
    text = readFileSync(path, "utf8")
  } catch {
    return 0
  }
  if (!text) return 0
  const lines = text.split("\n").length
  return text.endsWith("\n") ? lines - 1 : lines
}
const covers = (ranges: [number, number][], total: number) => {
  let next = 1
  for (const [start, end] of [...ranges].sort((a, b) => a[0] - b[0])) {
    if (start > next) break
    next = Math.max(next, end + 1)
  }
  return next > total
}

// "- /absolute/path" lines directly after the last "EGM required reads:" line.
const declaredReads = (prompt: unknown) => {
  const text = String(prompt ?? "")
  const at = text.lastIndexOf(READS_HEADER)
  if (at < 0) return []
  const paths: string[] = []
  for (const line of text.slice(at + READS_HEADER.length).split("\n").map((value) => value.trim())) {
    if (line.startsWith("- ")) paths.push(line.slice(2).trim())
    else if (line || paths.length) break
  }
  return paths
}

export default {
  id: "egm-guard",
  async setup(ctx: any) {
    const { primary, bundle, reviewers } = ctx.options ?? {}
    if (typeof primary !== "string" || typeof bundle !== "string" || !reviewers || !Object.keys(reviewers).length) {
      throw new Error("egm-guard: missing installer options; reviewer dispatch stays denied")
    }
    const bundleRoot = realpathSync(bundle)
    const isReviewer = (agent: unknown): agent is string => typeof agent === "string" && Object.hasOwn(reviewers, agent)

    const systems = new Map<string, string>()
    const systemOf = async (agent: string) => {
      if (!systems.has(agent)) {
        const info = await ctx.agent.get({ agentID: agent })
        systems.set(agent, info?.data?.system ?? info?.system ?? "")
      }
      return systems.get(agent)!
    }
    const directories = new Map<string, string>()
    const directoryOf = async (sessionID: string) => {
      if (!directories.has(sessionID)) {
        const session = await ctx.session.get({ sessionID })
        directories.set(sessionID, session?.location?.directory ?? ctx.location.directory)
      }
      return directories.get(sessionID)!
    }
    // The Location and its project worktree are internal to OpenCode; the bundle is the one allowed external root.
    const rootsOf = async (sessionID: string) => {
      const directory = real(await directoryOf(sessionID))
      const project = real(ctx.location?.project?.directory ?? "")
      return [directory, directory && project && within(directory, project) ? project : undefined, bundleRoot].filter(
        (root): root is string => !!root,
      )
    }
    const resolve = (path: string, directory: string) => (isAbsolute(path) ? path : join(directory, path))

    const isolate = (strict: boolean) => async (event: any) => {
      if (!isReviewer(event.agent)) return
      const own = await systemOf(event.agent)
      event.system = (event.system ?? []).flatMap((part: Part) => {
        const sources = part.metadata?.experimental?.instructions
        if (!Array.isArray(sources)) return own && part.text === own ? [part] : []
        const kept = sources.filter((source: any) => KEEP.has(source.key))
        const metadata = { ...part.metadata, experimental: { ...part.metadata.experimental, instructions: kept } }
        return kept.length ? [{ ...part, text: kept.map((source: any) => source.text).join("\n\n"), metadata }] : []
      })
      // Without its own system prompt the reviewer has no EGM instructions; fail rather than run.
      if (strict && !event.system.some((part: Part) => part.text === own)) {
        throw new Error(`EGM: ${event.agent} system prompt not found; the guard does not recognize this OpenCode request`)
      }
      // Instruction files, notifications and plugin injections arrive as synthetic messages.
      const history = await ctx.session.context({ sessionID: event.sessionID })
      const synthetic = new Set(history.filter((message: any) => message.type === "synthetic").map((message: any) => message.id))
      event.messages = (event.messages ?? []).filter((message: any) => !synthetic.has(message.id))
      for (const tool of Object.keys(event.tools ?? {})) if (!REVIEWER_TOOLS.has(tool)) delete event.tools[tool]
    }
    // Per-dispatch grants as session rules (sessionID -> tool call ID -> reviewer). Updates are
    // serialized because reviewers run concurrently.
    const live = new Map<string, Map<string, string>>()
    const synced = new Set<string>()
    let queue: Promise<unknown> = Promise.resolve()
    const sync = (sessionID: string) => {
      const task = async () => {
        const session = await ctx.session.get({ sessionID })
        const others = (session?.permissions ?? []).filter(
          (rule: Rule) => !(rule.action === "subagent" && isReviewer(rule.resource) && rule.effect === "allow"),
        )
        const ours = [...new Set(live.get(sessionID)?.values() ?? [])].map((resource) => ({ action: "subagent", resource, effect: "allow" }))
        await ctx.session.update({ sessionID, permissions: [...others, ...ours] })
        synced.add(sessionID)
      }
      queue = queue.then(task, task)
      return queue
    }

    const contextHook = isolate(true)
    await ctx.session.hook("context", async (event: any) => {
      // No foreground dispatch runs while the parent calls the model, so leftover grants are stale.
      // execute.after does not fire for calls that fail before execution, and a restart loses state.
      if (event.agent === primary && (live.get(event.sessionID)?.size || !synced.has(event.sessionID))) {
        live.get(event.sessionID)?.clear()
        await sync(event.sessionID)
      }
      await contextHook(event)
    })
    await ctx.session.hook("compaction", isolate(false))
    await ctx.session.hook("generate", isolate(false))

    await ctx.tool.hook("execute.before", async (event: any) => {
      if (event.tool !== "subagent" || event.agent !== primary || !isReviewer(event.input?.agent)) return
      const reads = declaredReads(event.input.prompt)
      if (!reads.length || reads.some((path) => !isAbsolute(path))) {
        throw new Error(
          `EGM: end the reviewer prompt with "${READS_HEADER}" followed by one "- <absolute path>" line for each file ` +
            "the stage requires the reviewer to read (target, standards, supplied reports).",
        )
      }
      // Every review is a fresh foreground child: never resume a reviewer session.
      const { sessionID: _session, background: _background, ...input } = event.input
      event.input = input
      if (!live.has(event.sessionID)) live.set(event.sessionID, new Map())
      live.get(event.sessionID)!.set(event.id, input.agent)
      await sync(event.sessionID)
    })

    await ctx.tool.hook("execute.after", async (event: any) => {
      if (event.tool !== "subagent" || event.agent !== primary || !isReviewer(event.input?.agent)) return
      const agent = event.input.agent
      if (live.get(event.sessionID)?.delete(event.id)) await sync(event.sessionID)
      if (event.status !== "completed") return
      const child = event.result?.metadata?.sessionID
      const ranges = new Map<string, [number, number][]>()
      if (child) {
        const directory = await directoryOf(child)
        for (const message of await ctx.session.context({ sessionID: child })) {
          if (message.type !== "assistant") continue
          for (const part of message.content ?? []) {
            if (part.type !== "tool" || part.name !== "read" || part.state?.status !== "completed") continue
            const target = real(resolve(String(part.state.input?.path ?? ""), directory))
            const text = part.state.content?.find((content: any) => content.type === "text")?.text ?? ""
            const range = readRange(text)
            if (target && range) ranges.set(target, [...(ranges.get(target) ?? []), range])
          }
        }
      }
      const missing = [reviewers[agent], ...declaredReads(event.input.prompt)].filter((path: string) => {
        const target = real(path)
        if (!target) return true
        const total = lineCount(target)
        return total === 0 || !covers(ranges.get(target) ?? [], total)
      })
      if (!missing.length) return
      const notice =
        `EGM: incomplete review. ${agent} did not read these required files in full: ${missing.join(", ")}. ` +
        "Do not use this review; report it as incomplete."
      event.result = {
        ...event.result,
        output: { ...event.result?.output, output: notice },
        content: [{ type: "text", text: notice }],
        metadata: { ...event.result?.metadata, egmMissingReads: missing },
      }
    })

    // Project config and session rules can append allows after the installer's reviewer rules;
    // configured denies never reach this hook, so it only tightens.
    await ctx.permission.hook("evaluate", async (event: any) => {
      const resources: string[] = event.resources ?? []
      // Reviewers serve only the EGM orchestrator, whose dispatches the hooks above check.
      if (event.action === "subagent" && event.agent !== primary && resources.some(isReviewer)) {
        event.effect = "deny"
        event.message = "EGM reviewers can only be dispatched by the egm agent."
        return
      }
      if (!isReviewer(event.agent)) return
      let permitted = event.action === "glob"
      if (event.action === "external_directory") {
        permitted = resources.every((resource) => {
          const target = real(resource.replace(/\/\*$/, ""))
          return !!target && within(target, bundleRoot)
        })
      }
      if (event.action === "read") {
        const directory = await directoryOf(event.sessionID)
        const roots = await rootsOf(event.sessionID)
        permitted = resources.every((resource) => {
          const requested = resolve(resource, directory)
          const target = real(requested)
          // A missing file cannot leak; the read itself reports it.
          if (!target) return !secret(relative(directory, requested))
          const root = roots.find((candidate) => within(target, candidate))
          return !!root && !secret(relative(directory, requested)) && !secret(relative(root, target))
        })
      }
      if (!permitted) {
        event.effect = "deny"
        event.message = "EGM reviewers may only read the project and the EGM bundle, excluding .env files."
      }
    })
  },
}
