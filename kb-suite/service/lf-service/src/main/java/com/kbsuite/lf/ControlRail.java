package com.kbsuite.lf;

import com.openjiuwen.core.singleagent.rail.AgentCallbackContext;
import com.openjiuwen.core.singleagent.rail.AgentRail;

import java.util.Map;

/**
 * Connects a run to its task: the task's steering inbox and its cancel flag.
 *
 * <p>Runs before loop-forest's SteeringProvisionRail (priority 1), which only binds a
 * fresh queue when none is bound, so /inject reaches the loop through {@link LfTask#steering()}.
 * A cancelled task force-finishes before its next model call, so cancelling stops within
 * the model call or tool that is already running.
 */
final class ControlRail extends AgentRail {

    private final LfTask task;
    private final boolean bindSteering;

    ControlRail(LfTask task, boolean bindSteering) {
        this.task = task;
        this.bindSteering = bindSteering;
        setPriority(90);
    }

    @Override
    public void beforeInvoke(AgentCallbackContext ctx) {
        if (bindSteering && !ctx.hasSteeringQueue()) {
            ctx.bindSteeringQueue(task.steering());
        }
    }

    @Override
    public void beforeModelCall(AgentCallbackContext ctx) {
        if (task.cancelRequested()) {
            ctx.requestForceFinish(Map.of("reason", "user_cancelled", "cancelled", true));
        }
    }
}
