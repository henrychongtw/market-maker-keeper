#!/usr/bin/env python3
#
# This file is part of Maker Keeper Framework.
#
# Copyright (C) 2017 reverendus
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.

import argparse
import operator
from functools import reduce
from typing import List

import logging

from api import Address
from api.approval import directly
from api.etherdelta import EtherDelta, Order
from api.feed import DSValue
from api.numeric import Wad
from api.oasis import OfferInfo
from keepers.sai import SaiKeeper


class SaiMakerEtherDelta(SaiKeeper):
    """SAI keeper to act as a market maker on EtherDelta.

    TODO work in progress
    """
    def __init__(self):
        super().__init__()
        self.max_eth_amount = Wad.from_number(self.arguments.max_eth_amount)
        self.min_eth_amount = Wad.from_number(self.arguments.min_eth_amount)
        self.max_sai_amount = Wad.from_number(self.arguments.max_sai_amount)
        self.min_sai_amount = Wad.from_number(self.arguments.min_sai_amount)
        self.eth_reserve = Wad.from_number(self.arguments.eth_reserve)
        self.min_margin = self.arguments.min_margin
        self.avg_margin = self.arguments.avg_margin
        self.max_margin = self.arguments.max_margin

        self.etherdelta_address = Address(self.config.get_contract_address("etherDelta"))
        self.etherdelta = EtherDelta(web3=self.web3, address=self.etherdelta_address)

    def args(self, parser: argparse.ArgumentParser):
        parser.add_argument("--min-margin", help="Minimum margin allowed", type=float, required=True)
        parser.add_argument("--avg-margin", help="Target margin, used on new order creation", type=float, required=True)
        parser.add_argument("--max-margin", help="Maximum margin allowed", type=float, required=True)
        parser.add_argument("--eth-reserve", help="Minimum amount of ETH to keep in order to cover gas", type=float, required=True)
        parser.add_argument("--max-eth-amount", help="Maximum value of open ETH sell orders", type=float, required=True)
        parser.add_argument("--min-eth-amount", help="Minimum value of open ETH sell orders", type=float, required=True)
        parser.add_argument("--max-sai-amount", help="Maximum value of open SAI sell orders", type=float, required=True)
        parser.add_argument("--min-sai-amount", help="Minimum value of open SAI sell orders", type=float, required=True)

    def startup(self):
        self.approve()
        self.on_block(self.synchronize_orders)
        self.every(60*60, self.print_balances)

    def shutdown(self):
        self.cancel_all_orders()

    def print_balances(self):
        def balances():
            for token in [self.sai]:
                yield f"{token.balance_of(self.our_address)} {token.name()}"
        logging.info(f"Keeper balances are {', '.join(balances())}.")
        #TODO PRINT ETH BALANCE

    def approve(self):
        """Approve EtherDelta to access our SAI, so we can deposit it"""
        self.etherdelta.approve([self.sai], directly())

    def our_orders(self):
        return list(filter(lambda order: order.user == self.our_address, self.etherdelta.active_onchain_orders()))

    def our_buy_orders(self):
        return list(filter(lambda order: order.token_get == self.sai.address and
                                         order.token_give == EtherDelta.ETH_TOKEN, self.our_orders()))

    def our_sell_orders(self):
        return list(filter(lambda order: order.token_get == EtherDelta.ETH_TOKEN and
                                         order.token_give == self.sai.address, self.our_orders()))

    def synchronize_orders(self):
        """Update our positions in the order book to reflect settings."""
        self.cancel_excessive_buy_orders()
        self.cancel_excessive_sell_orders()
        self.create_new_buy_offer()
        self.create_new_sell_offer()

    def cancel_excessive_buy_orders(self):
        """Cancel buy orders with rates outside allowed margin range."""
        for order in self.our_buy_orders():
            rate = self.rate_buy(order)
            rate_min = self.apply_buy_margin(self.target_rate(), self.min_margin)
            rate_max = self.apply_buy_margin(self.target_rate(), self.max_margin)
            if (rate < rate_max) or (rate > rate_min):
                self.etherdelta.cancel_order(order)

    def cancel_excessive_sell_orders(self):
        """Cancel sell orders with rates outside allowed margin range."""
        for order in self.our_sell_orders():
            rate = self.rate_sell(order)
            rate_min = self.apply_sell_margin(self.target_rate(), self.min_margin)
            rate_max = self.apply_sell_margin(self.target_rate(), self.max_margin)
            if (rate < rate_min) or (rate > rate_max):
                self.etherdelta.cancel_order(order)

    def cancel_all_orders(self):
        """Cancel all our orders."""
        for order in self.our_orders():
            self.etherdelta.cancel_order(order)

    def create_new_buy_offer(self):
        """If our ETH engagement is below the minimum amount, create a new offer up to the maximum amount"""
        total_amount = self.total_amount(self.our_buy_orders())
        if total_amount < self.min_eth_amount:
            our_balance = self.eth_balance(self.our_address) + self.etherdelta.balance_of(self.our_address) - self.eth_reserve
            have_amount = Wad.min(self.max_eth_amount, our_balance) - total_amount
            if have_amount > Wad(0):
                want_amount = have_amount / self.apply_buy_margin(self.target_rate(), self.avg_margin)
                self.etherdelta.place_order_onchain(token_get=self.sai.address, amount_get=want_amount,
                                                    token_give=EtherDelta.ETH_TOKEN, amount_give=have_amount,
                                                    expires=self.web3.eth.blockNumber+100)

    def create_new_sell_offer(self):
        """If our SAI engagement is below the minimum amount, create a new offer up to the maximum amount"""
        total_amount = self.total_amount(self.our_sell_orders())
        if total_amount < self.min_sai_amount:
            our_balance = self.sai.balance_of(self.our_address) + self.etherdelta.balance_of_token(self.sai.address, self.our_address)
            have_amount = Wad.min(self.max_sai_amount, our_balance) - total_amount
            if have_amount > Wad(0):
                want_amount = have_amount * self.apply_sell_margin(self.target_rate(), self.avg_margin)
                self.etherdelta.place_order_onchain(token_get=EtherDelta.ETH_TOKEN, amount_get=want_amount,
                                                    token_give=self.sai.address, amount_give=have_amount,
                                                    expires=self.web3.eth.blockNumber+100)

    def target_rate(self) -> Wad:
        ref_per_gem = Wad(DSValue(web3=self.web3, address=self.tub.pip()).read_as_int())
        return self.tub.par() / ref_per_gem

    @staticmethod
    def rate_buy(order: Order) -> Wad:
        return order.amount_give / order.amount_get

    @staticmethod
    def rate_sell(order: Order) -> Wad:
        return order.amount_get / order.amount_give

    def total_amount(self, orders: List[Order]):
        give_available = lambda order: order.amount_give - (self.etherdelta.amount_filled(order) * order.amount_give / order.amount_get)
        return reduce(operator.add, map(give_available, orders), Wad(0))

    @staticmethod
    def apply_buy_margin(rate: Wad, margin: float) -> Wad:
        return rate * Wad.from_number(1 - margin)

    @staticmethod
    def apply_sell_margin(rate: Wad, margin: float) -> Wad:
        return rate * Wad.from_number(1 + margin)


if __name__ == '__main__':
    SaiMakerEtherDelta().start()
