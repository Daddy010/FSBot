"""Holds main match classes"""
# External Imports
import discord
import asyncio
from logging import getLogger
from enum import Enum

# Internal Imports
import modules.discord_obj as d_obj
from display import AllStrings as disp, embeds, views
import modules.tools as tools
from classes.players import Player, ActivePlayer
import modules.database as db
import modules.accounts_handler as accounts

log = getLogger('fs_bot')

MATCH_TIMEOUT_TIME = 600
MATCH_WARN_TIME = 300
_match_id_counter = 0


class MatchState(Enum):
    INVITING = "Waiting for an invite to be accepted..."
    GETTING_READY = "Waiting for players to log in..."
    PLAYING = "Currently playing..."
    ENDED = "Match ended..."


class BaseMatch:
    _active_matches = dict()
    _recent_matches = dict()

    def __init__(self, owner: Player, player: Player):
        global _match_id_counter
        _match_id_counter += 1
        self.__id = _match_id_counter
        self.owner = owner
        self.__invited = list()
        self.start_stamp = tools.timestamp_now()
        self.end_stamp = None
        self.timeout_stamp = None
        self.__players: list[ActivePlayer] = [owner.on_playing(self),
                                              player.on_playing(self)]  # player list, add owners active_player
        self.__previous_players: list[Player] = list()
        self.match_log = list()  # logs recorded as list of tuples, (timestamp, message)
        self.status = MatchState.GETTING_READY
        self.text_channel: discord.TextChannel | None = None
        self.info_message: discord.Message | None = None
        self.embed_cache: discord.Embed | None = None
        BaseMatch._active_matches[self.id] = self

    @classmethod
    def active_matches_list(cls):
        return BaseMatch._active_matches.values()

    @classmethod
    async def create(cls, owner: Player, invited: Player):
        global _match_id_counter  # init _match_id_counter if first match created
        if not _match_id_counter:
            last_match = await db.async_db_call(db.get_last_element, 'matches')
            if last_match:
                _match_id_counter = last_match['_id']
        obj = cls(owner, invited)

        overwrites = {
            d_obj.guild.default_role: discord.PermissionOverwrite(view_channel=False),
            d_obj.roles['app_admin']: discord.PermissionOverwrite(view_channel=True),
            d_obj.roles['admin']: discord.PermissionOverwrite(view_channel=True),
            d_obj.roles['mod']: discord.PermissionOverwrite(view_channel=True),
            d_obj.guild.get_member(owner.id): discord.PermissionOverwrite(view_channel=True),
            d_obj.guild.get_member(invited.id): discord.PermissionOverwrite(view_channel=True)
        }

        obj.text_channel = await d_obj.categories['user'].create_text_channel(
            name=f'match-id𝄆{obj.id}𝄇-casual',
            overwrites=overwrites)
        obj.embed_cache = embeds.match_info(obj)

        obj.info_message = await disp.MATCH_INFO.send(obj.text_channel, match=obj,
                                                      view=views.MatchInfoView(obj))

        obj.log(f'Owner: {owner.name}[{owner.id}]')
        return obj

    async def join_match(self, player: Player):
        #  Joins player to match and updates permissions
        self.__invited.remove(player)
        self.__players.append(player.on_playing(self))
        await self.channel_update(player, True)
        self.log(f'{player.name} joined the match')

    async def leave_match(self, player: ActivePlayer):
        self.__previous_players.append(player.player)
        self.__players.remove(player)
        await self.channel_update(player, False)
        player.player.on_quit()
        self.log(f'{player.name} left the match')
        await disp.MATCH_LEAVE.send_temp(self.text_channel, p.mention)
        if not self.__players and not self.end_stamp:  # if no players left, and match not already ended
            await self.end_match()
        if player.account:
            await accounts.terminate_account(player=player.player)
            player.player.set_account(None)

    async def end_match(self):
        self.end_stamp = tools.timestamp_now()
        self.status = MatchState.ENDED
        self.update_status()
        await self.update_embed()
        await disp.MATCH_END.send(self.text_channel, self.id)
        with self.text_channel.typing():
            await asyncio.sleep(10)
        for player in self.__players:
            await self.leave_match(player)
        await self.text_channel.delete(reason='Match Ended')
        self.log('Match Ended')
        await db.async_db_call(db.set_element, 'matches', self.id, self.get_data())
        del BaseMatch._active_matches[self.id]
        BaseMatch._recent_matches[self.id] = self

    def get_data(self):
        player_ids = [player.id for player in self.__players and self.__previous_players]
        data = {'_id': self.id, 'start_stamp': self.start_stamp, 'end_stamp': self.end_stamp,
                'owner': self.owner.id, 'players': player_ids, 'match_log': self.match_log}
        return data

    async def channel_update(self, player, action: bool):
        player_member = d_obj.guild.get_member(player.id)
        await self.text_channel.set_permissions(player_member, view_channel=action)

    async def update_embed(self):
        if self.info_message:
            new_embed = embeds.match_info(self)
            if not tools.compare_embeds(new_embed, self.embed_cache):
                self.embed_cache = new_embed
                await disp.MATCH_INFO.edit(self.info_message, embed=new_embed, view=views.MatchInfoView(self))

    def update_status(self):
        if len(self.players) < 2:
            self.status = MatchState.INVITING
        elif len(self.online_players) < 2:
            self.status = MatchState.GETTING_READY
        elif self.online_players:
            self.status = MatchState.PLAYING

    async def update_match(self):
        # check timeout, reset if new match or online_players
        if self.online_players or self.start_stamp < tools.timestamp_now() - MATCH_WARN_TIME:
            self.timeout_stamp = None
        else:
            if not self.timeout_stamp:  # set timeout stamp
                self.timeout_stamp = tools.timestamp_now()
            elif self.should_warn:  # Warn of timeout
                await disp.MATCH_TIMEOUT_WARN.send(self.text_channel, self.all_mentions, delete_after=30)
            elif self.should_timeout:  # Timeout Match
                self.log("Match timed out for inactivity...")
                await disp.MATCH_TIMEOUT.send(self.text_channel, self.all_mentions)
                await self.end_match()

        self.update_status()
        await self.update_embed()

    def log(self, message):
        self.match_log.append((tools.timestamp_now(), message))
        log.info(f'Match ID [{self.id}]: {message}')

    @property
    def id(self):
        return self.__id

    @id.setter
    def id(self, value):
        if not value >= _match_id_counter:
            raise ValueError('Match ID\'s must be equal to / higher than the Match Id Counter, %s', _match_id_counter)
        self.__id = value

    @property
    def players(self):
        return self.__players

    @property
    def prev_players(self):
        return self.__previous_players

    @property
    def invited(self):
        return self.__invited

    @property
    def online_players(self):
        return [p for p in self.__players if p.online_id]

    @property
    def all_mentions(self):
        return [p.mention for p in self.__players if p.online_id]

    @property
    def timeout_at(self):
        if not self.timeout_stamp:
            return False
        return self.timeout_stamp + MATCH_TIMEOUT_TIME if self.timeout_stamp else False

    @property
    def should_warn(self):
        if not self.timeout_stamp:
            return False
        return True if self.timeout_stamp < tools.timestamp_now() - MATCH_WARN_TIME else False

    @property
    def should_timeout(self):
        if not self.timeout_stamp:
            return False
        return True if self.timeout_stamp < tools.timestamp_now() - MATCH_TIMEOUT_TIME else False

    def invite(self, player: Player):
        if player not in self.__invited:
            self.__invited.append(player)

    def decline_invite(self, player: Player):
        if player in self.__invited:
            self.__invited.remove(player)

