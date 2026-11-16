# SPDX-License-Identifier: MIT
import random

from lapinou.brain.base import is_strict_compatible
from lapinou.rabbit.platform import Step
from lapinou.tools.base import ToolBox, ToolContext
from lapinou.tools.stories import StoryTools

CTX = ToolContext(rabbit_id="lapin-a")


async def test_story_quiz_riddle_return_instruction_and_choreography():
    stories = StoryTools(rng=random.Random(1))
    box = ToolBox(stories.tools())
    for name, args in [("start_story", {"theme": "un dragon gentil"}), ("quiz", {}),
                       ("riddle", {})]:
        result = await box.call(name, args, CTX)
        assert not result.is_error and result.content
        assert result.choreography and all(isinstance(s, Step) for s in result.choreography)
        for step in result.choreography:
            if step.ears is not None:
                assert all(0 <= e <= 16 for e in step.ears)
            if step.pulse is not None:
                assert step.led is not None
    story = await box.call("start_story", {"theme": "un dragon gentil"}, CTX)
    assert "un dragon gentil" in story.content


async def test_specs_are_strict():
    for tool in StoryTools().tools():
        assert is_strict_compatible(tool.spec().input_schema)


async def test_riddles_do_not_repeat_immediately():
    stories = StoryTools(rng=random.Random(0))
    box = ToolBox(stories.tools())
    seen = [(await box.call("riddle", {}, CTX)).content for _ in range(4)]
    assert len(set(seen)) == 4
