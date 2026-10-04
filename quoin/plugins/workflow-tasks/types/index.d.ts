export type TaskKind = 'task' | 'program'

export type GateVerdict = 'pass' | 'fail' | 'undecided'

/** Every state the stage derivation can land on; `unknown` marks a task whose folder could not be read. */
export type StageKey =
  | 'run-active'
  | 'started'
  | 'pre-spec'
  | 'spec'
  | 'spec-approved'
  | 'spec-gate-failed'
  | 'architecture'
  | 'architecture-approved'
  | 'architecture-gate-failed'
  | 'planning'
  | 'plan-approved'
  | 'plan-gate-failed'
  | 'implement-done'
  | 'implement-gate-failed'
  | 'review'
  | 'review-approved'
  | 'review-gate-failed'
  | 'gate-undecided'
  | 'end-of-task-done'
  | 'stages-done'
  | 'program'
  | 'done'
  | 'unknown'

/** The fields of a run-state record the pane shows. */
export type RunState = {
  phase: string
  subphase: string
  step: string
  nextAction: string
  profile: string
  updatedAt: string
  resumeCommand: string
}

export type StageInfo = {
  key: StageKey
  /** Row text: `plan`, `impl ✓`, `review 2`. */
  short: string
  /** Detail-block text for the current stage. */
  label: string
  /** The stage number evaluated for a multi-stage task. */
  stage: number | null
  multi: boolean
  /** Rows of the architecture's stage decomposition, when it has any. */
  stageCount: number | null
  /** The detect_phase result the stage was refined from. */
  basePhase: string
  reviewRounds: number
  /** Whether a gate-undecided command names the task or the stage. */
  scope: 'task' | 'stage'
  /** The newest gate file the stage was refined from, with its verdict. */
  gate: { name: string; verdict: GateVerdict } | null
  /** The active run record, when one decided the stage. */
  run: RunState | null
  /** The record is older than the newest artifact. */
  runStale: boolean
  /** What the artifacts say, shown beside an active run record. */
  artifact: { key: StageKey; short: string; label: string } | null
}

export type NextCommand = {
  command: string | null
  /** The phase that follows the command. */
  then: string | null
  note: string | null
}

export type TaskRow = {
  /** The task argument a command takes: `foo` or `parent/child`. */
  arg: string
  name: string
  depth: number
  kind: TaskKind
  /** Latest activity of the task and everything under it, ms since the epoch. */
  activity: number
  stage: StageInfo
  next: NextCommand
}

/** A discovered task folder before its stage is derived. */
export type TaskNode = {
  /** Folder name. */
  name: string
  /** The task argument: `foo` or `parent/child`. */
  arg: string
  kind: TaskKind
  /** Latest artifact change in the folder itself, ms since the epoch. */
  activity: number
  /** Latest change in the folder and every child. */
  subtree: number
  children: TaskNode[]
  /** The folder could not be listed. */
  failed: boolean
}

/** What the pane draws from; the same four values `$.state` holds under the plugin's name. */
export type PaneState = {
  rows: TaskRow[]
  root: string | null
  selected: string | null
  error: string | null
}

declare module 'claude-code' {
  interface PluginState {
    'workflow-tasks': {
      rows: TaskRow[]
      root: string | null
      selected: string | null
      error: string | null
    }
  }
}
