import asyncio
import importlib.util
import os
import sys
from collections import deque
from pathlib import Path

MODULE_PATH = Path(__file__).with_name('server.py')
spec = importlib.util.spec_from_file_location('snake_server_under_test', MODULE_PATH)
server = importlib.util.module_from_spec(spec)
assert spec.loader is not None
sys.modules[spec.name] = server
spec.loader.exec_module(server)

async def new_game():
    manager = server.GameManager()
    game = server.Game(manager, game_id='TEST1', warmup_seconds=0, desired_bots=0)
    game.loop_task.cancel()
    try:
        await game.loop_task
    except asyncio.CancelledError:
        pass
    game.snakes.clear()
    game.food.clear()
    game.chat_history.clear()
    return game

def add_snake(game, snake_id, body, direction='right', bot=False):
    snake = server.Snake(
        snake_id=snake_id,
        nickname=snake_id,
        color='#ffffff',
        body=deque(body),
        direction=direction,
        pending_direction=direction,
        bot=bot,
    )
    game.snakes[snake_id] = snake
    return snake

async def test_body_segment_behind_head_is_solid():
    game = await new_game()
    victim = add_snake(game, 'victim', [(10, 10), (9, 10), (8, 10)], 'right')
    attacker = add_snake(game, 'attacker', [(9, 9), (9, 8), (9, 7)], 'down')
    game._step_snakes()
    assert victim.alive, 'body owner should survive'
    assert not attacker.alive, 'attacker must die when hitting segment directly behind another head'
    assert attacker.death_reason == 'body'
    assert attacker.killed_by == 'victim'
    assert victim.length == 4, 'body owner should receive ceil(30%) reward from length 3 attacker'

async def test_old_head_becomes_body_after_movement():
    game = await new_game()
    victim = add_snake(game, 'victim', [(10, 10), (9, 10), (8, 10)], 'right')
    attacker = add_snake(game, 'attacker', [(10, 9), (10, 8), (10, 7)], 'down')
    game._step_snakes()
    assert victim.alive
    assert not attacker.alive, 'old head should be lethal body when it remains after movement'
    assert attacker.death_reason == 'body'
    assert attacker.killed_by == 'victim'

async def test_vacating_tail_is_not_solid():
    game = await new_game()
    victim = add_snake(game, 'victim', [(10, 10), (9, 10), (8, 10)], 'right')
    tail_chaser = add_snake(game, 'tailchaser', [(8, 9), (8, 8), (8, 7)], 'down')
    game._step_snakes()
    assert victim.alive
    assert tail_chaser.alive, 'a tail cell that leaves the final body should not be lethal'

async def test_self_collision():
    game = await new_game()
    snake = add_snake(game, 'selfie', [(5, 5), (5, 6), (4, 6), (4, 5), (4, 4)], 'left')
    game._step_snakes()
    assert not snake.alive
    assert snake.death_reason == 'self'

async def test_wall_collision():
    game = await new_game()
    snake = add_snake(game, 'wall', [(0, 5), (1, 5), (2, 5)], 'left')
    game._step_snakes()
    assert not snake.alive
    assert snake.death_reason == 'wall'

async def test_head_to_head_same_cell():
    game = await new_game()
    a = add_snake(game, 'a', [(5, 5), (4, 5), (3, 5)], 'right')
    b = add_snake(game, 'b', [(7, 5), (8, 5), (9, 5)], 'left')
    game._step_snakes()
    assert not a.alive and not b.alive
    assert a.death_reason == 'head'
    assert b.death_reason == 'head'

async def test_head_to_head_swap():
    game = await new_game()
    a = add_snake(game, 'a', [(5, 5), (4, 5), (3, 5)], 'right')
    b = add_snake(game, 'b', [(6, 5), (7, 5), (8, 5)], 'left')
    game._step_snakes()
    assert not a.alive and not b.alive
    assert a.death_reason == 'head'
    assert b.death_reason == 'head'

async def test_sprint_path_hits_body():
    game = await new_game()
    victim = add_snake(game, 'victim', [(6, 4), (6, 5), (6, 6)], 'up')
    sprinter = add_snake(game, 'sprinter', [(4, 5), (3, 5), (2, 5), (1, 5), (1, 6), (1, 7)], 'right')
    sprinter.pending_sprint = True
    game._step_snakes()
    assert victim.alive
    assert not sprinter.alive
    assert sprinter.death_reason == 'body'
    assert sprinter.killed_by == 'victim'

async def test_sprint_intermediate_cells_become_body():
    game = await new_game()
    sprinter = add_snake(game, 'sprinter', [(4, 5), (3, 5), (2, 5), (1, 5), (1, 6), (1, 7)], 'right')
    sprinter.pending_sprint = True
    attacker = add_snake(game, 'attacker', [(6, 4), (6, 3), (6, 2)], 'down')
    game._step_snakes()
    assert sprinter.alive
    assert not attacker.alive, 'intermediate sprint path cell should be body by tick end'
    assert attacker.death_reason == 'body'
    assert attacker.killed_by == 'sprinter'

async def test_debug_ssl_is_disabled_by_env():
    old_debug = os.environ.get('SNAKE_DEBUG')
    old_ssl = os.environ.get('SNAKE_SSL')
    try:
        os.environ['SNAKE_DEBUG'] = '1'
        os.environ.pop('SNAKE_SSL', None)
        assert server.debug_mode_enabled()
        ctx, reason = server.build_ssl_context()
        assert ctx is None
        assert 'debug mode' in reason
    finally:
        if old_debug is None:
            os.environ.pop('SNAKE_DEBUG', None)
        else:
            os.environ['SNAKE_DEBUG'] = old_debug
        if old_ssl is None:
            os.environ.pop('SNAKE_SSL', None)
        else:
            os.environ['SNAKE_SSL'] = old_ssl

async def main():
    tests = [
        test_body_segment_behind_head_is_solid,
        test_old_head_becomes_body_after_movement,
        test_vacating_tail_is_not_solid,
        test_self_collision,
        test_wall_collision,
        test_head_to_head_same_cell,
        test_head_to_head_swap,
        test_sprint_path_hits_body,
        test_debug_ssl_is_disabled_by_env,
    ]
    for test in tests:
        await test()
        print(f'PASS {test.__name__}')

if __name__ == '__main__':
    asyncio.run(main())
