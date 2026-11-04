// Publication is available after the required modeling stages through validation.
export const PUBLISH_REQUIRED_STAGES = ['research', 'object', 'operation', 'validate'];

export function missingPublishStages(stages, statuses) {
  return stages.filter((stage, index) =>
    PUBLISH_REQUIRED_STAGES.includes(stage.key) && statuses[index] !== 'finish');
}
