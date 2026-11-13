import { describe, it } from 'node:test';
import assert from 'node:assert/strict';
import { getNextSkill, PIPELINE, PHASE_TO_NODE, NEXT_SKILL } from '../workflowMapping';

describe('workflowMapping', () => {
  describe('PIPELINE', () => {
    it('has exactly 6 nodes', () => {
      assert.strictEqual(PIPELINE.length, 6);
    });

    it('contains the canonical node names', () => {
      assert.deepStrictEqual([...PIPELINE], [
        'discover', 'architect', 'thorough_plan', 'implement', 'review', 'end_of_task',
      ]);
    });
  });

  describe('PHASE_TO_NODE', () => {
    it('maps all 10 known detect_phase outputs', () => {
      const expected: Record<string, string> = {
        'discover':        'discover',
        'architecture':    'architect',
        'planning':        'thorough_plan',
        'plan-gated':      'thorough_plan',
        'implement':       'implement',
        'implement-gated': 'implement',
        'review':          'review',
        'review-gated':    'review',
        'committed':       'end_of_task',
        'done':            'end_of_task',
      };
      assert.deepStrictEqual(PHASE_TO_NODE, expected);
    });
  });

  describe('getNextSkill', () => {
    it('null → null', () => {
      assert.strictEqual(getNextSkill(null), null);
    });

    it('unknown node → null', () => {
      assert.strictEqual(getNextSkill('nonexistent'), null);
    });

    it('discover → architect', () => {
      assert.strictEqual(getNextSkill('discover'), 'architect');
    });

    it('architect → thorough_plan', () => {
      assert.strictEqual(getNextSkill('architect'), 'thorough_plan');
    });

    it('thorough_plan → implement', () => {
      assert.strictEqual(getNextSkill('thorough_plan'), 'implement');
    });

    it('implement → review', () => {
      assert.strictEqual(getNextSkill('implement'), 'review');
    });

    it('review → end_of_task', () => {
      assert.strictEqual(getNextSkill('review'), 'end_of_task');
    });

    it('end_of_task → null (defensive dead path)', () => {
      assert.strictEqual(getNextSkill('end_of_task'), null);
    });
  });

  describe('NEXT_SKILL coverage', () => {
    it('all non-terminal PIPELINE nodes have a next skill', () => {
      for (const node of PIPELINE.slice(0, -1)) {
        assert.ok(NEXT_SKILL[node] !== undefined, `${node} missing from NEXT_SKILL`);
      }
    });
  });
});
