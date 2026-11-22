// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {ZeroCapOmniExecutor} from "../src/ZeroCapOmniExecutor.sol";
import {
    IMorphoLiquidateCallback,
    IMorphoFlashLoanCallback,
    MarketParams
} from "../src/interfaces/IMorphoBlue.sol";
import {ISwapRouter02} from "../src/interfaces/ISwapRouter.sol";

contract MockERC20 {
    string public name;
    string public symbol;
    uint8 public decimals = 18;
    mapping(address => uint256) public balanceOf;
    mapping(address => mapping(address => uint256)) public allowance;

    function init(string memory _n, string memory _s) external {
        name = _n;
        symbol = _s;
    }

    function mint(address to, uint256 amount) external {
        balanceOf[to] += amount;
    }

    function approve(address spender, uint256 amount) external returns (bool) {
        allowance[msg.sender][spender] = amount;
        return true;
    }

    function transfer(address to, uint256 amount) external returns (bool) {
        require(balanceOf[msg.sender] >= amount, "Insufficient bal");
        balanceOf[msg.sender] -= amount;
        balanceOf[to] += amount;
        return true;
    }

    function transferFrom(address from, address to, uint256 amount) external returns (bool) {
        require(balanceOf[from] >= amount, "Insufficient bal");
        require(allowance[from][msg.sender] >= amount, "Insufficient allowance");
        allowance[from][msg.sender] -= amount;
        balanceOf[from] -= amount;
        balanceOf[to] += amount;
        return true;
    }
}

contract MockSwapRouter {
    uint256 public rateMultiplierBps = 10500;

    function setRateMultiplierBps(uint256 _bps) external {
        rateMultiplierBps = _bps;
    }

    function exactInputSingle(ISwapRouter02.ExactInputSingleParams calldata params)
        external
        payable
        returns (uint256 amountOut)
    {
        MockERC20(params.tokenIn).transferFrom(msg.sender, address(this), params.amountIn);
        amountOut = (params.amountIn * rateMultiplierBps) / 10000;
        require(amountOut >= params.amountOutMinimum, "Router: slippage exceeded");
        MockERC20(params.tokenOut).mint(params.recipient, amountOut);
    }
}

contract MockMorpho {
    function liquidate(
        MarketParams calldata params,
        address,
        uint256 seizedAssets,
        uint256 repaidShares,
        bytes calldata data
    ) external returns (uint256, uint256) {
        uint256 collateralToSend = seizedAssets > 0 ? seizedAssets : 100 ether;
        uint256 debtToRepay = repaidShares > 0 ? 100 ether : 95 ether;

        MockERC20(params.collateralToken).mint(msg.sender, collateralToSend);
        IMorphoLiquidateCallback(msg.sender).onMorphoLiquidate(debtToRepay, data);
        MockERC20(params.loanToken).transferFrom(msg.sender, address(this), debtToRepay);
        return (collateralToSend, debtToRepay);
    }

    function flashLoan(address token, uint256 assets, bytes calldata data) external {
        MockERC20(token).mint(msg.sender, assets);
        IMorphoFlashLoanCallback(msg.sender).onMorphoFlashLoan(assets, data);
        MockERC20(token).transferFrom(msg.sender, address(this), assets);
    }
}

contract MockDutchAuction {
    MockERC20 public payToken;
    uint256 public price;
    bool public initialized;

    function init(MockERC20 _payToken, uint256 _price) external {
        payToken = _payToken;
        price = _price;
        initialized = true;
    }

    function buy(address[] calldata assets, address receiver, bytes calldata) external returns (uint256) {
        payToken.transferFrom(msg.sender, address(this), price);
        for (uint256 i = 0; i < assets.length; i++) {
            MockERC20(assets[i]).mint(receiver, 100 ether);
        }
        return price;
    }
}

contract MockBeefyStrategy {
    MockERC20 public weth;
    uint256 public rewardAmount;
    bool public initialized;

    function init(MockERC20 _weth, uint256 _reward) external {
        weth = _weth;
        rewardAmount = _reward;
        initialized = true;
    }

    function harvest(address callFeeRecipient) external {
        weth.mint(callFeeRecipient, rewardAmount);
    }
}

contract ZeroCapOmniExecutorTest {
    ZeroCapOmniExecutor public executor;
    MockERC20 public usdc;
    MockERC20 public weth;
    MockSwapRouter public router;
    MockMorpho public morpho;
    MockDutchAuction public auction;
    MockBeefyStrategy public strategy;

    address public owner;

    function setUp() public {
        owner = address(0xBEEF);
        usdc = new MockERC20();
        weth = new MockERC20();
        router = new MockSwapRouter();
        morpho = new MockMorpho();
        auction = new MockDutchAuction();
        strategy = new MockBeefyStrategy();

        usdc.init("USD Coin", "USDC");
        weth.init("Wrapped Ether", "WETH");
        auction.init(usdc, 100 ether);
        strategy.init(weth, 0.01 ether);

        address[] memory initialTargets = new address[](2);
        initialTargets[0] = address(auction);
        initialTargets[1] = address(strategy);

        executor = new ZeroCapOmniExecutor(
            owner,
            address(morpho),
            address(router),
            address(weth),
            initialTargets
        );
    }

    function testProfitableMorphoLiquidation() public {
        MarketParams memory mp = MarketParams({
            loanToken: address(usdc),
            collateralToken: address(weth),
            oracle: address(0x1111),
            irm: address(0x2222),
            lltv: 860000000000000000
        });

        usdc.mint(address(executor), 50 ether);

        uint256 ownerBefore = usdc.balanceOf(owner);
        uint256 profit = executor.executeMorphoLiquidation(mp, address(0xCAFE), 0, 100 ether, 500, 4 ether);
        require(profit == 5 ether, "Expected 5 ether liquidation profit delta");
        require(usdc.balanceOf(owner) - ownerBefore == 5 ether, "Owner did not receive exact profit delta");
    }

    function testMorphoLiquidationRevertsIfBelowMinProfit() public {
        MarketParams memory mp = MarketParams({
            loanToken: address(usdc),
            collateralToken: address(weth),
            oracle: address(0x1111),
            irm: address(0x2222),
            lltv: 860000000000000000
        });

        (bool ok, ) = address(executor).call(
            abi.encodeWithSelector(
                executor.executeMorphoLiquidation.selector,
                mp,
                address(0xCAFE),
                0,
                100 ether,
                500,
                6 ether
            )
        );
        require(!ok, "Should have reverted on InsufficientProfit");
    }

    function testProfitableDutchAuctionWithSlippageGuards() public {
        address[] memory rewards = new address[](1);
        rewards[0] = address(weth);
        uint24[] memory fees = new uint24[](1);
        fees[0] = 500;
        uint256[] memory minOuts = new uint256[](1);
        minOuts[0] = 104 ether;

        uint256 ownerBefore = usdc.balanceOf(owner);
        uint256 profit = executor.executeDutchAuction(
            address(auction),
            address(usdc),
            100 ether,
            rewards,
            100 ether,
            fees,
            minOuts,
            4 ether
        );
        require(profit == 5 ether, "Expected 5 ether auction profit");
        require(usdc.balanceOf(owner) - ownerBefore == 5 ether, "Owner did not receive auction profit");
    }

    function testProfitableHarvestBountyAndAllowlistGuard() public {
        uint256 ownerBefore = weth.balanceOf(owner);
        uint256 bounty = executor.executeHarvestBounty(address(strategy), 0.005 ether);
        require(bounty == 0.01 ether, "Expected 0.01 WETH bounty");
        require(weth.balanceOf(owner) - ownerBefore == 0.01 ether, "Owner did not receive WETH bounty");

        (bool ok, ) = address(executor).call(
            abi.encodeWithSelector(executor.executeHarvestBounty.selector, address(0xDEAD), 0.001 ether)
        );
        require(!ok, "Unallowed strategy must revert");
    }

    function testRevertOnUnauthorizedCallbacks() public {
        (bool ok1, ) = address(executor).call(
            abi.encodeWithSelector(executor.onMorphoLiquidate.selector, 100 ether, bytes(""))
        );
        require(!ok1, "Direct onMorphoLiquidate call must revert");

        (bool ok2, ) = address(executor).call(
            abi.encodeWithSelector(executor.onMorphoFlashLoan.selector, 100 ether, bytes(""))
        );
        require(!ok2, "Direct onMorphoFlashLoan call must revert");
    }

    function runAllTests() external returns (uint256 passedCount) {
        setUp();
        testProfitableMorphoLiquidation();
        passedCount++;

        setUp();
        testMorphoLiquidationRevertsIfBelowMinProfit();
        passedCount++;

        setUp();
        testProfitableDutchAuctionWithSlippageGuards();
        passedCount++;

        setUp();
        testProfitableHarvestBountyAndAllowlistGuard();
        passedCount++;

        setUp();
        testRevertOnUnauthorizedCallbacks();
        passedCount++;
    }
}
