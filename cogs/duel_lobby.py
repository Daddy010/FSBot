"""
Cog built to handle interaction with the duel lobby.

"""
# External Imports
import discord
from discord.ext import commands, tasks
from datetime import datetime as dt, timedelta
from logging import getLogger

# Internal Imports
import modules.config as cfg
import modules.discord_obj as d_obj
from modules.spam_detector import is_spam
from classes.players import Player
from classes.match import BaseMatch
from display import AllStrings as disp, embeds, views


import modules.tools as tools

log = getLogger('fs_bot')

_bot: discord.Bot = None

_lobbied_players: list[Player] = []


class ChallengeDropdown(discord.ui.Select):
    def __init__(self):
        options = []
        for player in _lobbied_players:
            option = discord.SelectOption(label=player.name, value=str(player.id))
            options.append(option)

        super().__init__(placeholder="Pick Player(s) in the lobby to challenge...",
                         custom_id='dashboard-challenge',
                         options=options,
                         min_values=1,
                         max_values=len(options),
                         )

    async def callback(self, inter: discord.Interaction):
        player: Player = Player.get(inter.user.id)
        if await is_spam(inter, inter.user) or not await d_obj.is_registered(inter, player):
            return
        invited_players: list(Player) = [Player.get(int(value)) for value in self.values]
        if player in invited_players:
            await disp.LOBBY_INVITED_SELF.send_temp(inter, player.mention)
            return
        if player and not player.match:
            if player in _lobbied_players:
                _cog.lobby_leave(player)
            match = await BaseMatch.create(player, invited_players)
            invited_string = ' '.join([p.mention for p in invited_players])
            await disp.MATCH_INFO.send(match.voice_channel, match=match)
            await disp.MATCH_INVITED.send(match.voice_channel, invited_string, match.owner.mention, view=views.InviteView(match))
            _cog.log(f"Match {match.id} created by {match.owner.name}, invited: {','.join([p.name for p in invited_players])}")
            await _cog.update_dashboard()
            await disp.MATCH_CREATE.send_temp(inter.response, player.mention, match.id)

class DashboardView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        if _lobbied_players:
            self.add_item(ChallengeDropdown())

    @discord.ui.button(label="Join Lobby", custom_id='dashboard-join', style=discord.ButtonStyle.green)
    async def join_lobby_button(self, button: discord.Button, inter: discord.Interaction):
        player: Player = Player.get(inter.user.id)
        if await is_spam(inter, inter.user) or not await d_obj.is_registered(inter, player):
            return
        elif _cog.lobby_join(player):
            await _cog.update_dashboard()
            await disp.LOBBY_JOIN.send_temp(inter, player.mention)
        else:
            await disp.LOBBY_ALREADY_IN.send_temp(inter, player.mention)

    @discord.ui.button(label="Reset Timeout", custom_id='dashboard-reset', style=discord.ButtonStyle.blurple)
    async def reset_lobby_button(self, button: discord.Button, inter: discord.Interaction):
        player: Player = Player.get(inter.user.id)
        if await is_spam(inter, inter.user) or not await d_obj.is_registered(inter, player):
            return
        elif player in _lobbied_players:
            _cog.lobby_timeout_reset(player)
            await disp.LOBBY_TIMEOUT_RESET.send_temp(inter, player.mention)
        else:
            await disp.LOBBY_NOT_IN.send_temp(inter, player.mention)

    @discord.ui.button(label="Extended History", custom_id='dashboard-history', style=discord.ButtonStyle.blurple)
    async def history_lobby_button(self, button:discord.Button, inter: discord.Interaction):
        if await is_spam(inter, inter.user):
            return
        if len(_cog.lobby_logs) <= len(_cog.logs_recent):
            await disp.LOBBY_NO_HISTORY.send_temp(inter, inter.user.mention)
            return
        await disp.LOBBY_LONGER_HISTORY.send(inter, inter.user.mention, logs=_cog.logs_longer, delete_after=20)

    @discord.ui.button(label="Leave Lobby", custom_id='dashboard-leave', style=discord.ButtonStyle.red)
    async def leave_lobby_button(self, button: discord.Button, inter: discord.Interaction):
        player: Player = Player.get(inter.user.id)
        if await is_spam(inter, inter.user) or not await d_obj.is_registered(inter, player):
            return
        elif _cog.lobby_leave(player):
            await _cog.update_dashboard()
            await disp.LOBBY_LEAVE.send_temp(inter, player.mention)
        else:
            await disp.LOBBY_NOT_IN.send_temp(inter, player.mention)


class DuelLobbyCog(commands.Cog, name="DuelLobbyCog", command_attrs=dict(guild_ids=[cfg.general['guild_id']],
                                                                         default_permission=True)):
    def __init__(self, bot):
        #  Statics
        self.bot = bot
        self.dashboard_channel: discord.TextChannel = d_obj.channels['dashboard']
        # Dynamics
        self.dashboard_msg: discord.Message = None

        self.lobby_logs: list[(int, str)] = []  # lobby logs recorded as a list of tuples, (timestamp, message)
        self.recent_log_length: int = 8
        self.longer_log_length: int = 25

        self.timeout_minutes: int = 30
        self._warned_players: list[Player] = []

        self.dashboard_loop.start()

    def cog_check(self, ctx):
        player = Player.get(ctx.user.id)
        return True if player else False

    @property
    def logs_recent(self):
        return self.lobby_logs[-self.recent_log_length:]

    @property
    def logs_longer(self):
        return self.lobby_logs[-self.longer_log_length:]

    def log(self, message):
        self.lobby_logs.append((tools.timestamp_now(), message))
        log.info(f'Lobby Log: {message}')

    def lobby_timeout(self, player):
        """Removes from lobby list, executes player lobby leave method, returns True if removed"""
        if player in _lobbied_players:
            player.on_lobby_leave()
            _lobbied_players.remove(player)
            self._warned_players.remove(player)
            self.log(f'{player.name} was removed from the lobby by timeout.')
            return True
        else:
            return False

    def lobby_timeout_reset(self, player):
        """Resets player lobbied timestamp, returns True if player was in lobby"""
        if player in _lobbied_players:
            player.reset_lobby_timestamp()
            if player in self._warned_players:
                self._warned_players.remove(player)
            return True
        return False

    def lobby_leave(self, player):
        """Removes from lobby list, executes player lobby leave method, returns True if removed"""
        if player in _lobbied_players:
            player.on_lobby_leave()
            _lobbied_players.remove(player)
            self.log(f'{player.name} left the lobby.')
            return True
        else:
            return False

    def lobby_join(self, player):
        """Adds to lobby list, executes player lobby join method, returns True if added"""
        if player not in _lobbied_players:
            player.on_lobby_add()
            _lobbied_players.append(player)
            self.log(f'{player.name} joined the lobby.')
            return True
        else:
            return False

    def dashboard_purge_check(self, message: discord.Message):
        """Checks if messages are either the dashboard message, or an admin message before purging them"""
        if message != self.dashboard_msg and d_obj.is_not_admin(message.author):
            return True
        else:
            return False

    async def create_dashboard(self):
        """Purges the channel, and then creates dashboard Embed w/ view"""
        await self.dashboard_channel.purge(check=self.dashboard_purge_check)
        self.dashboard_msg = await self.dashboard_channel.send(content="",
                                                               embed=embeds.duel_dashboard(_lobbied_players,
                                                                                           self.logs_recent),
                                                               view=DashboardView())

    async def update_dashboard(self):
        """Checks if dashboard exists and either creates one, or updates the current dashboard and purges messages older than 5 minutes"""
        if not self.dashboard_msg:
            await self.create_dashboard()
            return
        await d_obj.channels['dashboard'].purge(before=(dt.now() - timedelta(minutes=5)),
                                                check=self.dashboard_purge_check)
        await self.dashboard_msg.edit(embed=embeds.duel_dashboard(_lobbied_players, self.logs_recent),
                                      view=DashboardView())

    # async def create_match(self, creator, players: list[Player]):
    #     match = BaseMatch.create(creator, players)
    #     await asyncio.sleep(2)
    #     self.log(f'Match: {match.id} created by {match.owner.name}')
    #     return match

    @tasks.loop(seconds=10)
    async def dashboard_loop(self):
        """Loop to check lobby timeouts, also updates dashboard in-case preference changes are made"""
        for p in _lobbied_players:
            stamp_dt = dt.fromtimestamp(p.lobbied_timestamp)
            if stamp_dt < (dt.now() - timedelta(minutes=self.timeout_minutes)):
                self.lobby_timeout(p)
                await disp.LOBBY_TIMEOUT.send(self.dashboard_channel, p.mention, delete_after=30)
            elif stamp_dt < (dt.now() - timedelta(minutes=self.timeout_minutes - 5)) and p not in self._warned_players:
                self._warned_players.append(p)
                self.log(f'{p.name} will soon be timed out of the lobby')
                await disp.LOBBY_TIMEOUT_SOON.send(self.dashboard_channel, p.mention, delete_after=30)

        await self.update_dashboard()


_cog: DuelLobbyCog = None


def setup(client: discord.Bot):
    client.add_cog(DuelLobbyCog(client))
    global _bot
    _bot = client
    global _cog
    _cog = client.cogs.get('DuelLobbyCog')
